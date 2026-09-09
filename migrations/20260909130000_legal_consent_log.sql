-- Nice-to-haves on top of 20260909120000_users_legal_consent.sql (HCL-5):
--   * the IP address alongside the timestamp
--   * an append-only log with one row per acceptance
-- The columns on public.users stay as the "current acceptance" for easy querying;
-- public.legal_consents is the history behind them.

alter table public.users
  add column if not exists legal_accepted_ip text;

comment on column public.users.legal_accepted_ip is
  'Client IP of the most recent acceptance, from X-Forwarded-For where present. See public.legal_consents for the full history.';

create table if not exists public.legal_consents (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users (id) on delete cascade,
  version     text not null,
  ip_address  text,
  accepted_at timestamptz not null default now(),
  -- One acceptance per user per document version. Also makes the recording
  -- endpoint safe to call twice (retry, double submit) without a second row.
  unique (user_id, version)
);

comment on table public.legal_consents is
  'One row per acceptance of the AGB/Datenschutzerklärung/AVV. Append-only, written by the backend only (service role).';

create index if not exists legal_consents_user_id_idx
  on public.legal_consents (user_id);

alter table public.legal_consents enable row level security;
