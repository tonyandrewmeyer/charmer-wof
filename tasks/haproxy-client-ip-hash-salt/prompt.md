Add a `client-ip-hash-salt` config option to this charm, so operators can keep client IP addresses out of HAProxy's logs. The option is of type `secret`: the ID of a user-owned Juju secret whose `salt` key holds the salt. Requirements:

- When the option is set, HAProxy logs a salted SHA-256 hash of the client address instead of the address itself, in the `log-format` and `error-log-format` of the `defaults` section and in the `log-format` of TCP frontends. The client address part of each format becomes exactly `%[src,concat(,,),regsub(^::ffff:,),concat(,,\"<salt>\"),sha2(256),hex]:%cp` (with `<salt>` the secret's value); keep the rest of HAProxy's default formats as they are. When the option is unset, logging is unchanged (plaintext client addresses).
- Validate the secret before rendering anything, and set the unit to blocked (without writing the configuration) with these messages:
  - `The client-ip-hash-salt secret does not exist or cannot be accessed.` if the secret can't be read;
  - `The client-ip-hash-salt secret must contain a non-empty 'salt' value.` if the `salt` key is missing, empty or only whitespace;
  - `The client-ip-hash-salt secret must not contain control characters, double quotes, backslashes, or dollar signs.` for any of those characters (DEL included).
- Reconcile when the secret's content changes.
- The `get-configuration` action must not reveal the salt: replace it with `<redacted>` in the configuration it returns, wherever it appears in a hash expression, even if it differs from the current secret's value.
- Keep the existing unit tests passing, and add unit tests for the new behaviour.
- Run the linters and fix anything they report.

Follow the charm's existing structure and conventions. Work in this directory. Do not ask questions; make reasonable decisions and finish the task.
