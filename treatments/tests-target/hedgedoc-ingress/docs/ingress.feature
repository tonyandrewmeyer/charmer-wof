Feature: HedgeDoc behind a Traefik ingress
  HedgeDoc computes a single serverURL at startup from CMD_DOMAIN,
  CMD_PROTOCOL_USESSL and CMD_URL_PATH, and builds every generated link, its
  stylesheet URLs and the websocket origin from it. When HedgeDoc is behind
  Traefik, these must describe the address Traefik serves it on.
  This file is a human-readable statement of the behaviour; it is not executed.
  The executable form is tests/unit/test_acceptance_ingress.py and
  tests/unit/test_acceptance_ingress_exemplar.py.

  Background:
    Given the charm has a "requires" endpoint named "ingress" using the "ingress" interface
    And the charm requests ingress with the charms.traefik_k8s.v2.ingress library
    And Traefik publishes its assigned URL in its application databag as
      """
      {"ingress": "{\"url\": \"<scheme>://<host>[:port]/<model>-<app>\"}"}
      """

  Scenario: An http ingress URL with a sub-path
    Given an ingress relation whose URL is "http://traefik.example.com/mymodel-hedgedoc-k8s"
    When the charm reconciles
    Then the HedgeDoc service environment has CMD_DOMAIN "traefik.example.com"
    And CMD_PROTOCOL_USESSL is false
    And CMD_URL_PATH is "mymodel-hedgedoc-k8s" (no leading or trailing slash)
    And CMD_URL_ADDPORT, if set, is false

  Scenario: An https ingress URL
    Given an ingress relation whose URL is "https://pad.example.com/mymodel-hedgedoc-k8s"
    When the charm reconciles
    Then CMD_DOMAIN is "pad.example.com"
    And CMD_PROTOCOL_USESSL is true
    And CMD_URL_PATH is "mymodel-hedgedoc-k8s"

  Scenario: A non-default port
    Given an ingress relation whose URL is "http://10.0.0.5:8080/mymodel-hedgedoc-k8s"
    When the charm reconciles
    Then CMD_DOMAIN is "10.0.0.5:8080"
    And CMD_URL_ADDPORT, if set, is false

  Scenario: The ingress URL takes precedence over configuration
    Given the "external-hostname" config option is "configured.example.org"
    And "external-tls" is false
    And an ingress relation whose URL is "https://pad.example.com/mymodel-hedgedoc-k8s"
    When the charm reconciles
    Then CMD_DOMAIN is "pad.example.com"
    And CMD_PROTOCOL_USESSL is true
    And CMD_URL_PATH is "mymodel-hedgedoc-k8s"

  Scenario: The ingress URL is applied when the relation data changes
    Given an ingress relation whose URL is "https://pad.example.com/mymodel-hedgedoc-k8s"
    When the ingress relation's data changes
    Then CMD_DOMAIN is "pad.example.com"
    And CMD_URL_PATH is "mymodel-hedgedoc-k8s"

  Scenario: Ingress is revoked
    Given the "external-hostname" config option is "configured.example.org"
    And "external-tls" is true
    And an ingress relation whose URL is "https://pad.example.com/mymodel-hedgedoc-k8s"
    When the ingress relation is broken
    Then CMD_DOMAIN is "configured.example.org"
    And CMD_PROTOCOL_USESSL is true
    And CMD_URL_PATH is unset or empty

  Scenario: The charm requests ingress from Traefik
    Given the unit is the leader in model "mymodel"
    And an ingress relation
    When the ingress relation's data changes
    Then the charm's application databag on the relation has model "mymodel"
    And name "hedgedoc-k8s"
    And port 3000
    And strip-prefix true, so that Traefik removes "/<model>-<app>" before forwarding

  Scenario: Status with ingress
    Given an ingress relation whose URL is "https://pad.example.com/mymodel-hedgedoc-k8s"
    When the charm reconciles
    Then the unit status is active

  Scenario: Traefik has not published a URL yet
    Given the "external-hostname" config option is "configured.example.org"
    And an ingress relation with no data from Traefik
    When the charm reconciles
    Then CMD_DOMAIN is "configured.example.org"
    And the unit status is active or waiting

  Scenario: The catalogue item links to the ingress URL
    Given an ingress relation whose URL is "https://pad.example.com/hedgedoc"
    And a catalogue relation
    When the charm reconciles
    Then the catalogue item is named "HedgeDoc" and its URL is "https://pad.example.com/hedgedoc"

  Scenario: The OAuth redirect URI follows the ingress URL
    Given an oauth relation
    And an ingress relation whose URL is "https://pad.example.com/hedgedoc"
    When the charm reconciles
    Then the charm publishes the redirect URI "https://pad.example.com/hedgedoc/auth/oauth2/callback"

  Scenario: Ingress takes precedence over the in-cluster fallback address
    Given no "external-hostname" config option
    And an ingress relation whose URL is "https://pad.example.com/hedgedoc"
    When the charm reconciles
    Then CMD_DOMAIN is "pad.example.com"
