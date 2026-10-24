-- Daily-digest support for lead notifications.
ALTER TABLE contact_requests
  ADD COLUMN IF NOT EXISTS digest_sent_at timestamptz;
