-- Google Ads joins the project integration registry as an identifier-entry connection: the
-- founder enters a customer id, Tin's manager account sends the invitation, and no customer
-- credential is stored. Constraints are restated in full; the custom.api.* form stays.
ALTER TABLE integration_connections DROP CONSTRAINT integration_connections_provider_key_check;
ALTER TABLE integration_connections ADD CONSTRAINT integration_connections_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');

ALTER TABLE integration_call_receipts DROP CONSTRAINT integration_call_receipts_provider_key_check;
ALTER TABLE integration_call_receipts ADD CONSTRAINT integration_call_receipts_provider_key_check
    CHECK (provider_key IN ('analytics.gsc', 'infra.github', 'workspace.google', 'ads.google')
           OR provider_key ~ '^custom\.api\.[a-z][a-z0-9_]{0,47}$');
