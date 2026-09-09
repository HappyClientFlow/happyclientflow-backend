-- Legal consent (AGB / Datenschutzerklärung / AVV) captured at registration.
-- Required for law firms under § 43e BRAO: the DPA must be actively accepted,
-- a reference inside the terms is not enough. See HCL-5.

alter table public.users
  add column if not exists legal_accepted_at      timestamptz,
  add column if not exists legal_accepted_version text;

comment on column public.users.legal_accepted_at is
  'When the user ticked the AGB/Datenschutzerklärung/AVV checkbox on sign-up. Server-generated, set once and never overwritten.';

comment on column public.users.legal_accepted_version is
  'Version of the legal documents that was live when the user accepted, e.g. 2026-08. Sent by the frontend (LEGAL_DOCUMENTS_VERSION) so it reflects what the user actually saw.';
