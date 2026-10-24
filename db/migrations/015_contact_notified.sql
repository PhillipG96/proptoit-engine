-- Lead notification support: track which contact requests have been alerted.
ALTER TABLE contact_requests
  ADD COLUMN IF NOT EXISTS notified boolean NOT NULL DEFAULT false;
