# Edge probes run FROM the VM.
#
# /api/health always answers 200 — only its JSON body distinguishes a healthy
# instance from a broken one, so that instance asserts on the body and not on
# the status code. /readyz is the one endpoint whose status code carries
# meaning (200/503), so it is the readiness signal. include_content stays off
# so a response body never reaches Datadog.
#
# base_url and acme_host are decided by the module (TLS mode + domain), not
# here: this template stays a dumb renderer.
init_config: {}

instances:
  - name: agnes_readyz
    url: ${base_url}/readyz
    http_response_status_code: "200"
    timeout: 10
    include_content: false
    tls_verify: true
    min_collection_interval: 60
    tags:
      - instance:agnes_readyz

  - name: agnes_health_body
    url: ${base_url}/api/health
    http_response_status_code: "200"
    content_match: '"status":\s*"ok"'
    timeout: 10
    include_content: false
    tls_verify: true
    min_collection_interval: 60
    tags:
      - instance:agnes_health_body
%{ for h in acme_hosts ~}

  # Plain HTTP on port 80 must stay reachable and redirecting, or Let's Encrypt
  # cannot renew over HTTP-01 and the certificate silently ages out.
  - name: agnes_acme_http
    url: http://${h}/
    http_response_status_code: "(200|301|302|308)"
    allow_redirects: false
    timeout: 10
    include_content: false
    min_collection_interval: 60
    tags:
      - instance:agnes_acme_http
%{ endfor ~}
