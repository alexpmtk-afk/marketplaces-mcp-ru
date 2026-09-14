# Drive resumable bridge v3 hardening

Purpose: eliminate the large annual CSV failure mode without storing a Google OAuth refresh token in Yandex Cloud.

Production contract:
- Apps Script remains the Google authorization boundary and only brokers Drive control-plane operations.
- Large CSV bytes travel Yandex -> official Google Drive resumable session URI, never through Apps Script/base64.
- Resumable chunks are 256 KiB aligned, bounded to 32 MiB, 4 MiB by default.
- Google Drive `Range` is authoritative after interruption; bytes are never blindly resent.
- Non-rate-limit 4xx responses restart the resumable session; rate-limit 403/429 and transient 5xx use backoff/recovery.
- The opaque resumable session URI is treated as a bearer secret and is never logged or exposed.
- Production annual data uploads to a deterministic staging filename first.
- Drive file size + `sha256Checksum` must match the immutable Yandex candidate before promotion.
- A byte-for-byte Yandex backup is verified before canonical promotion/COMMIT.
- Apps Script promotion uses explicit staged/canonical file IDs and is retry-safe after an uncertain response.
- ID-based Apps Script actions are limited to the fixed archive root.
- Diagnostic cleanup can trash only diagnostic temp filenames.
- `setupBridge` performs a Drive REST preflight so API/scope problems appear before production deployment.

Laser Master safety:
- existing durable job stays at 23/38 until diagnostic upload PASS;
- report 743994450 PREPARE is never repeated;
- WB is not called again for the existing candidate;
- canonical annual file is untouched during staging upload;
- only after exact SHA256 verification and backup does promotion occur.
