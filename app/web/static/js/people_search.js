/* window.AgnesPeopleSearch — the one client-side "find an account by name
 * or email" lookup, shared by every admin surface that offers to add a
 * person to a group: the group drawer's creation-time People field
 * (js/components/group_drawer.js, "New group" on /admin/access and the
 * Add-data wizard's "Share with another group" step) and the group detail
 * pane's own "add someone" search (/admin/access, `.ax-people__find`).
 *
 * Both used to carry their own copy of the fetch + response-shape
 * handling — harmless while the two copies agreed, but nothing kept them
 * agreeing, and an edit to one was never guaranteed to reach the other.
 * One implementation now, so there is nothing left to drift.
 *
 * GET /api/users?search=<term>&limit=<n> — admin-only (app/api/users.py ::
 * list_users), case-insensitive substring match on email OR name
 * (`UserRepository.search_recent` / its Postgres sibling). Returns a bare
 * JSON array, never `{ users: [...] }`; the `.users` fallback below is
 * defensive against a future response-shape change, not a real branch
 * either endpoint takes today.
 */
window.AgnesPeopleSearch = {
  USERS_API: '/api/users',

  // Resolves to `{ people, error }`, never rejects. `error` is null on a
  // genuine (possibly empty) result — a caller renders "no account
  // matches" ONLY when `error` is null and `people` is empty. A non-ok
  // response or a network failure instead sets `error` to a short,
  // human-readable string and leaves `people` empty, so a 403/500/501 is
  // never silently indistinguishable from "nobody matched" — the bug that
  // hid a real outage behind a wrong "no such person" reading.
  search(query, limit) {
    var q = String(query == null ? '' : query).trim();
    if (!q) return Promise.resolve({ people: [], error: null });
    return window.AgnesPeopleSearch._get(
      window.AgnesPeopleSearch.USERS_API +
      '?search=' + encodeURIComponent(q) + '&limit=' + (limit || 8));
  },

  /* The same lookup with no term: the accounts to OFFER before anyone has
     typed. `search('')` deliberately resolves empty — an empty box is not a
     query — but a picker that shows nothing until you can spell a colleague's
     name is only usable by someone who already knows the answer, which is
     rarely true of the admin doing the adding. The endpoint is
     `search_recent`, so a bare limit is its natural no-term form.

     Same `{ people, error }` contract, for the same reason: a caller must be
     able to tell "this instance has nobody else" from "the lookup is down". */
  recent(limit) {
    return window.AgnesPeopleSearch._get(
      window.AgnesPeopleSearch.USERS_API + '?limit=' + (limit || 8));
  },

  //: One fetch + response-shape handler, so `search` and `recent` cannot
  //: drift the way the two hand-rolled copies of `search` once did.
  _get(url) {
    return fetch(url, { credentials: 'include' })
      .then(function (r) {
        if (r.ok) {
          return r.json().then(function (people) {
            if (!Array.isArray(people)) people = (people && people.users) || [];
            return { people: people, error: null };
          });
        }
        return r.json().catch(function () { return {}; }).then(function (body) {
          var detail = body && typeof body.detail === 'string' ? body.detail : r.statusText;
          var msg = 'HTTP ' + r.status + (detail ? ': ' + detail : '');
          return { people: [], error: msg };
        });
      })
      .catch(function () {
        return { people: [], error: 'network error' };
      });
  },
};
