# Certificate expiry for every public hostname this VM terminates TLS for,
# including any alias whose ACME account carries no contact e-mail and would
# therefore expire without a warning mail.
init_config: {}

instances:
%{ for h in hosts ~}
  - server: ${h}
    port: 443
    transport: TCP
    days_warning: 30
    days_critical: 14
    min_collection_interval: 900
    tags:
      - tls_target:${h}
%{ endfor ~}
