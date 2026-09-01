"""Unit tests for the deploy-time exposure scan (`src/data_apps/deploy_check.py`, #1946).

`check_tree` is the pure core exercised here — a `Mapping[str, str]` in, a
`CheckReport` out, no filesystem/git involved. `check_git_ref`'s own
git-reading integration is covered separately at the bottom.
"""

from __future__ import annotations

import subprocess

import pytest

from src.data_apps import deploy_check
from src.data_apps.deploy_check import check_git_ref, check_tree, skipped_report


def _rule_ids(report: dict) -> list[str]:
    return [f["rule_id"] for f in report["findings"]]


class TestCheckTreeShape:
    def test_empty_tree_passes(self):
        report = check_tree({})
        assert report["status"] == "pass"
        assert report["findings"] == []
        assert report["files_scanned"] == 0
        assert report["skipped"] is None
        assert set(report["rules_run"]) == {"DA001", "DA002", "DA003", "DA004", "DA005", "DA006"}

    def test_clean_file_passes(self):
        report = check_tree({"app.py": "print('hello')\n"})
        assert report["status"] == "pass"
        assert report["files_scanned"] == 1

    def test_a_finding_sets_warn_status(self):
        report = check_tree({"server/index.ts": "app.use(express.static(__dirname));\n"})
        assert report["status"] == "warn"
        assert report["findings"]

    def test_finding_shape(self):
        report = check_tree({"server/index.ts": "app.use(express.static(__dirname));\n"})
        finding = report["findings"][0]
        assert finding["rule_id"] == "DA001"
        assert finding["severity"] == "warn"
        assert finding["file"] == "server/index.ts"
        assert finding["line"] == 1
        assert "express.static" in finding["snippet"]
        assert finding["doc_url"].startswith("/docs/")

    def test_never_raises_on_bad_input(self):
        # `files` isn't even a mapping — `check_tree` degrades to a skipped
        # report rather than propagating.
        report = check_tree(None)  # type: ignore[arg-type]
        assert report["status"] == "skipped"
        assert report["skipped"] == "internal_error"

    def test_one_rule_failing_does_not_stop_the_others(self, monkeypatch):
        def _boom(path, lines):
            raise RuntimeError("boom")

        monkeypatch.setattr(deploy_check, "_check_da001", _boom)
        report = check_tree({"server/index.ts": "res.json(process.env);\n"})
        # DA001 raised and was swallowed; DA004 still ran and fired.
        assert "DA004" in _rule_ids(report)


class TestSkippedReport:
    def test_shape(self):
        report = skipped_report("external_repo")
        assert report == {
            "status": "skipped",
            "findings": [],
            "rules_run": [],
            "files_scanned": 0,
            "skipped": "external_repo",
        }


class TestWalkSkips:
    @pytest.mark.parametrize(
        "path",
        [
            "node_modules/left-pad/index.js",
            "packages/app/node_modules/left-pad/index.js",
            "dist/index.js",
            "server/dist/index.js",
            "build/main.py",
            "vendor/build/x.py",
        ],
    )
    def test_skip_directories(self, path):
        report = check_tree({path: "app.use(express.static(__dirname));\n"})
        assert report["files_scanned"] == 0
        assert report["findings"] == []

    def test_skip_minified_js(self):
        report = check_tree({"bundle.min.js": "app.use(express.static(__dirname));"})
        assert report["files_scanned"] == 0

    def test_skip_package_lock(self):
        report = check_tree({"package-lock.json": '{"AGNES_TOKEN": "res.json("}'})
        assert report["files_scanned"] == 0

    def test_skip_oversized_file(self):
        huge = "x" * (256 * 1024 + 1)
        report = check_tree({"server/index.ts": huge})
        assert report["files_scanned"] == 0

    def test_file_at_the_cap_is_scanned(self):
        content = "app.use(express.static('public'));\n" + " " * (200 * 1024)
        report = check_tree({"server/index.ts": content})
        assert report["files_scanned"] == 1


class TestDA001NodeStaticRoot:
    def test_bare_dirname_warns(self):
        report = check_tree({"server/index.ts": "app.use(express.static(__dirname));\n"})
        assert _rule_ids(report) == ["DA001"]

    def test_process_cwd_warns(self):
        report = check_tree({"server/index.ts": "app.use(express.static(process.cwd()));\n"})
        assert _rule_ids(report) == ["DA001"]

    @pytest.mark.parametrize("literal", [".", "/", "/app", ".."])
    def test_bare_root_marker_literals_warn(self, literal):
        report = check_tree({"server/index.ts": f'app.use(express.static("{literal}"));\n'})
        assert _rule_ids(report) == ["DA001"]

    def test_scaffold_shape_passes(self):
        """The CRITICAL false-positive constraint: the shipped scaffold's
        exact `path.resolve(__dirname, "..", "..", "dist")` indirection
        (app/initial_workspace_default/scaffolds/nodejs-dashboard/server/index.ts)
        must not fire — it always serves `dist/`, never an ancestor's tree."""
        content = 'const distDir = path.resolve(__dirname, "..", "..", "dist");\napp.use(express.static(distDir));\n'
        report = check_tree({"server/index.ts": content})
        assert report["findings"] == []

    def test_inline_path_join_named_subdir_passes(self):
        report = check_tree({"server/index.ts": 'app.use(express.static(path.join(__dirname, "public")));\n'})
        assert report["findings"] == []

    def test_path_resolve_ending_in_dotdot_warns(self):
        # Goes up but never scopes down into a name — serves the parent
        # directory itself.
        report = check_tree({"server/index.ts": 'app.use(express.static(path.resolve(__dirname, "..")));\n'})
        assert _rule_ids(report) == ["DA001"]

    def test_plain_relative_name_passes(self):
        report = check_tree({"server/index.ts": 'app.use(express.static("public"));\n'})
        assert report["findings"] == []

    def test_serve_static_alias_recognized(self):
        report = check_tree({"server/index.ts": "app.use(serveStatic(__dirname));\n"})
        assert _rule_ids(report) == ["DA001"]

    def test_fastify_static_registration_flagged(self):
        report = check_tree({"server/index.ts": "fastify.register(fastifyStatic, { root: process.cwd() });\n"})
        assert _rule_ids(report) == ["DA001"]

    def test_only_applies_to_node_files(self):
        # Same suspicious text, but in a Python file — DA001 doesn't fire
        # (DA002 owns Python static-root exposure).
        report = check_tree({"app.py": "# app.use(express.static(__dirname));\n"})
        assert report["findings"] == []


class TestDA002PythonStaticRoot:
    @pytest.mark.parametrize("value", ['"."', "os.getcwd()", '"/app"', '".."'])
    def test_static_files_bad_values_warn(self, value):
        report = check_tree({"app.py": f"app.mount('/static', StaticFiles(directory={value}), name='static')\n"})
        assert _rule_ids(report) == ["DA002"]

    def test_static_files_scoped_subdir_passes(self):
        report = check_tree({"app.py": "app.mount('/static', StaticFiles(directory='dist'), name='static')\n"})
        assert report["findings"] == []

    def test_send_from_directory_bad_value_warns(self):
        report = check_tree({"app.py": "return send_from_directory('..', filename)\n"})
        assert _rule_ids(report) == ["DA002"]

    def test_static_folder_bare_path_parent_warns(self):
        report = check_tree({"app.py": "app.static_folder = Path(__file__).parent\n"})
        assert _rule_ids(report) == ["DA002"]

    def test_static_folder_scoped_passes(self):
        report = check_tree({"app.py": 'app.static_folder = Path(__file__).parent / "static"\n'})
        assert report["findings"] == []

    def test_only_applies_to_python_files(self):
        report = check_tree({"server/index.ts": "// StaticFiles(directory='.')\n"})
        assert report["findings"] == []


class TestDA003NginxRootAlias:
    def test_root_app_warns(self):
        report = check_tree({"keboola-config/nginx/sites/default.conf": "server {\n    root /app;\n}\n"})
        assert _rule_ids(report) == ["DA003"]

    def test_root_bare_slash_warns(self):
        report = check_tree({"keboola-config/nginx/sites/default.conf": "root /;\n"})
        assert _rule_ids(report) == ["DA003"]

    def test_root_single_segment_mount_warns(self):
        report = check_tree({"keboola-config/nginx/sites/default.conf": "root /srv;\n"})
        assert _rule_ids(report) == ["DA003"]

    def test_root_scoped_subdir_passes(self):
        report = check_tree({"keboola-config/nginx/sites/default.conf": "root /app/dist;\n"})
        assert report["findings"] == []

    def test_alias_bad_value_warns(self):
        report = check_tree({"keboola-config/nginx/sites/default.conf": "alias /app;\n"})
        assert _rule_ids(report) == ["DA003"]

    def test_autoindex_on_warns(self):
        report = check_tree({"keboola-config/nginx/sites/default.conf": "autoindex on;\n"})
        assert _rule_ids(report) == ["DA003"]

    def test_proxy_pass_only_config_passes(self):
        report = check_tree(
            {"keboola-config/nginx/sites/default.conf": "location / {\n    proxy_pass http://127.0.0.1:3000;\n}\n"}
        )
        assert report["findings"] == []

    def test_only_applies_to_nginx_conf_path(self):
        # Same "root /;" text, but not under keboola-config/nginx/ — ignored.
        report = check_tree({"notes/root.conf": "root /;\n"})
        assert report["findings"] == []


class TestDA004EnvSerialization:
    def test_js_res_json_process_env_warns(self):
        report = check_tree({"server/index.ts": "res.json(process.env);\n"})
        assert _rule_ids(report) == ["DA004"]

    def test_js_json_stringify_process_env_warns(self):
        report = check_tree({"server/index.ts": "console.log(JSON.stringify(process.env));\n"})
        assert _rule_ids(report) == ["DA004"]

    def test_js_spread_process_env_warns(self):
        report = check_tree({"server/index.ts": "res.json({ ...process.env, ok: true });\n"})
        assert _rule_ids(report) == ["DA004"]

    def test_python_return_dict_os_environ_warns(self):
        report = check_tree({"app.py": "return dict(os.environ)\n"})
        assert _rule_ids(report) == ["DA004"]

    def test_python_return_str_os_environ_warns(self):
        report = check_tree({"app.py": "return str(os.environ)\n"})
        assert _rule_ids(report) == ["DA004"]

    def test_python_jsonify_dict_os_environ_warns(self):
        report = check_tree({"app.py": "return jsonify(dict(os.environ))\n"})
        assert _rule_ids(report) == ["DA004"]

    def test_reading_a_single_env_var_passes(self):
        report = check_tree({"server/index.ts": "const port = process.env.PORT;\n"})
        assert report["findings"] == []


class TestDA005AgnesTokenEcho:
    def test_res_json_leak_warns(self):
        report = check_tree({"server/index.ts": "res.json({ token: AGNES_TOKEN });\n"})
        assert _rule_ids(report) == ["DA005"]

    def test_jsonify_leak_warns(self):
        report = check_tree({"app.py": "return jsonify(token=AGNES_TOKEN)\n"})
        assert _rule_ids(report) == ["DA005"]

    def test_python_bare_dict_return_leak_warns(self):
        report = check_tree({"app.py": "return {'token': AGNES_TOKEN}, 200\n"})
        assert _rule_ids(report) == ["DA005"]

    def test_js_bare_object_return_is_not_a_response_sink(self):
        """`return {...}` in JS/TS is an ordinary function return (Express
        answers via `res.*`, never `return`) — flagging it would misfire on
        any helper that happens to build an object containing the token,
        exactly like the scaffold's own `authHeaders()`."""
        report = check_tree({"server/agnesQuery.ts": "return { token: AGNES_TOKEN };\n"})
        assert report["findings"] == []

    def test_legit_outbound_authorization_header_passes(self):
        """The scaffold's server/agnesQuery.ts builds an OUTBOUND request
        header with AGNES_TOKEN — never a response sink — and must stay
        clean (tests/test_data_apps_scaffold.py pins AGNES_TOKEN's legitimate
        use there)."""
        content = 'return { "Content-Type": "application/json", Authorization: `Bearer ${AGNES_TOKEN}` };\n'
        report = check_tree({"server/agnesQuery.ts": content})
        assert report["findings"] == []

    def test_agnes_url_is_not_scanned(self):
        """v1 scope: only the literal AGNES_TOKEN is checked — AGNES_URL/
        AGNES_APP_ID are not secrets and must never be flagged."""
        report = check_tree({"server/index.ts": "res.json({ url: AGNES_URL });\n"})
        assert report["findings"] == []

    def test_secret_names_argument_is_accepted_but_unused_in_v1(self):
        """v2 additivity: passing per-app secret names must not raise, and
        must not (yet) produce a finding for them."""
        report = check_tree({"server/index.ts": "res.json({ key: MY_APP_SECRET });\n"}, secret_names=["MY_APP_SECRET"])
        assert report["findings"] == []


class TestDA006DebugMode:
    def test_flask_debug_true_warns_info(self):
        report = check_tree({"app.py": "app.run(host='0.0.0.0', debug=True)\n"})
        assert report["findings"][0]["rule_id"] == "DA006"
        assert report["findings"][0]["severity"] == "info"

    def test_flask_debug_env_var_warns(self):
        report = check_tree({"app.py": "FLASK_DEBUG=1\n"})
        assert _rule_ids(report) == ["DA006"]

    def test_raw_error_object_response_warns(self):
        report = check_tree({"server/index.ts": "res.json(err);\n"})
        assert _rule_ids(report) == ["DA006"]

    def test_debug_false_passes(self):
        report = check_tree({"app.py": "app.run(host='0.0.0.0', debug=False)\n"})
        assert report["findings"] == []

    def test_a_warn_finding_and_an_info_finding_both_set_warn_status(self):
        """`status` is "warn" as soon as there's ANY finding — severity does
        not change the report-level status, only how a UI would style it."""
        report = check_tree({"app.py": "app.run(debug=True)\n"})
        assert report["status"] == "warn"


class TestCheckGitRef:
    def test_scans_the_tree_at_a_resolved_sha(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from src.data_apps.git_repos import init_app_repo, resolve_ref

        slug = "gitcheck"
        repo_dir = init_app_repo(slug)
        work = tmp_path / "work"
        subprocess.run(["git", "clone", str(repo_dir), str(work)], check=True, capture_output=True)
        (work / "server").mkdir()
        (work / "server" / "index.ts").write_text("app.use(express.static(__dirname));\n")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)

        sha = resolve_ref(slug, "main")
        report = check_git_ref(slug, sha)
        assert report["status"] == "warn"
        assert report["findings"][0]["file"] == "server/index.ts"

    def test_invalid_slug_degrades_to_skipped_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        report = check_git_ref("not a valid slug!", "deadbeef")
        assert report["status"] == "skipped"
        assert report["skipped"] == "read_error"
