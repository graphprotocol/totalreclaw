/**
 * L3 — phrase-safety. WebAuthn passkey enrol + PRF assertion wrappers.
 *
 * The `prf` extension returns a deterministic 32-byte secret (per passkey +
 * fixed salt) that wraps the SPA's keys (see wrap.ts). Uses navigator.credentials
 * directly for precise control over reading getClientExtensionResults().prf.
 *
 * INVARIANTS: never log/print/transmit the prf secret. UV ("userVerification:
 * required") gates every assertion behind biometric/PIN.
 */

/** Fixed PRF salt — see spa-phase1.md §3.1 + auth-flow note. Do not change
 *  (changing it invalidates every wrapped vault). */
const PRF_SALT = new TextEncoder().encode("tr-vault-wrap-v1");
const RP_NAME = "TotalReclaw";

/** Thrown when the platform/browser lacks usable `prf` output → hard-gate. */
export class PrfUnsupportedError extends Error {
  constructor() {
    super("This browser does not support the passkey PRF extension required by TotalReclaw.");
    this.name = "PrfUnsupportedError";
  }
}

function rpId(): string {
  return (globalThis as { location?: { hostname?: string } }).location?.hostname ?? "localhost";
}

/**
 * Copy bytes into a fresh ArrayBuffer-backed view. TS 5.7+ makes Uint8Array
 * generic over its backing buffer (Uint8Array<ArrayBufferLike>), but the DOM
 * WebAuthn types require BufferSource = ArrayBufferView<ArrayBuffer>. This
 * guarantees the concrete ArrayBuffer-backed type.
 */
function abBytes(src: Uint8Array) {
  const view = new Uint8Array(new ArrayBuffer(src.byteLength));
  view.set(src);
  return view;
}

/**
 * Enrol a new passkey for this origin with the prf extension requested.
 * Returns the credential id (for allowCredentials on later assertions).
 * Throws PrfUnsupportedError if the authenticator reports prf unsupported.
 */
export async function enrolPasskey(opts: {
  userId: Uint8Array;
  userName: string;
}): Promise<{ credentialId: Uint8Array }> {
  const challenge = abBytes(crypto.getRandomValues(new Uint8Array(32)));
  const cred = (await navigator.credentials.create({
    publicKey: {
      rp: { name: RP_NAME, id: rpId() },
      user: { id: abBytes(opts.userId), name: opts.userName, displayName: opts.userName },
      challenge,
      pubKeyCredParams: [
        { type: "public-key", alg: -7 }, // ES256
        { type: "public-key", alg: -257 }, // RS256
      ],
      authenticatorSelection: {
        residentKey: "required",
        userVerification: "required",
      },
      extensions: { prf: {} } as AuthenticationExtensionsClientInputs,
    },
  })) as PublicKeyCredential | null;
  if (!cred) throw new Error("Passkey enrolment was cancelled.");
  const ext = cred.getClientExtensionResults() as { prf?: { enabled?: boolean } };
  if (!ext.prf?.enabled) throw new PrfUnsupportedError();
  return { credentialId: new Uint8Array(cred.rawId) };
}

/**
 * Assert an existing passkey and read the 32-byte prf secret. UV is required
 * (biometric/PIN). `credentialId` scopes the assertion to one passkey; omit to
 * let the platform pick a resident credential.
 */
export async function getPrfSecret(opts?: {
  credentialId?: Uint8Array;
}): Promise<{ credentialId: Uint8Array; prfSecret: Uint8Array }> {
  const challenge = abBytes(crypto.getRandomValues(new Uint8Array(32)));
  const assertion = (await navigator.credentials.get({
    publicKey: {
      challenge,
      allowCredentials: opts?.credentialId
        ? [{ type: "public-key", id: abBytes(opts.credentialId) }]
        : undefined,
      userVerification: "required",
      extensions: {
        prf: { eval: { first: abBytes(PRF_SALT) } },
      } as AuthenticationExtensionsClientInputs,
    },
  })) as PublicKeyCredential | null;
  if (!assertion) throw new Error("Passkey unlock was cancelled.");
  const ext = assertion.getClientExtensionResults() as {
    prf?: { results?: { first?: ArrayBuffer } };
  };
  const first = ext.prf?.results?.first;
  if (!first) throw new PrfUnsupportedError();
  const prfSecret = new Uint8Array(first);
  if (prfSecret.length !== 32) throw new PrfUnsupportedError();
  return { credentialId: new Uint8Array(assertion.rawId), prfSecret };
}
