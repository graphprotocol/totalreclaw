/**
 * Input-schema fragment for the block-and-tell override on
 * `totalreclaw_remember` (PRD-04 DEP-4). The descriptions are the ones the
 * Hermes tool schema ships (`python/src/totalreclaw/hermes/schemas.py`).
 */
export const SENSITIVE_PARAM_SCHEMA = {
  allow_sensitive: {
    type: 'boolean',
    default: false,
    description:
      'Default false. Text that looks like a secret or sensitive ' +
      'identifier (API key, token, password/PIN, private key, ' +
      'recovery phrase, payment card, IBAN, government ID) is NOT ' +
      "stored: the result has blocked='sensitive', a message and " +
      'a confirm_token. Relay the message to the user and wait ' +
      'for their answer. Set true ONLY in a later turn, after the ' +
      'user explicitly confirmed, and pass the confirm_token. ' +
      'Without a valid confirm_token from an earlier turn, ' +
      'allow_sensitive is ignored.',
  },
  confirm_token: {
    type: 'string',
    description:
      'The confirm_token from a previous blocked result. ' +
      'Required with allow_sensitive=true.',
  },
} as const;
