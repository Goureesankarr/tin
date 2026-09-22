-- One Google Ads campaign per launch run and the monitor's approval-gated proposals.
-- Per-step provider effects (validate, create, subscriptions, enable, conversion action,
-- snippets, tag pull request) are receipted in effect_receipts under paid_ads_launch:{run}:apply:*.
CREATE TABLE paid_ads_campaigns (
    run_id uuid PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    integration_connection_id uuid
        REFERENCES integration_connections(id) ON DELETE SET NULL,
    customer_id text NOT NULL CHECK (customer_id ~ '^[0-9]{10}$'),
    mode text NOT NULL CHECK (mode IN ('launch', 'tracking', 'setup')),
    source_run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    source_commit_sha text NOT NULL CHECK (length(source_commit_sha) = 40),
    plan_path text NOT NULL,
    plan_commit_sha text NOT NULL CHECK (length(plan_commit_sha) = 40),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'creating', 'live', 'tracking', 'setup',
                          'failed', 'stopped')),
    daily_budget_micros bigint CHECK (daily_budget_micros IS NULL OR daily_budget_micros > 0),
    cpc_ceiling_micros bigint CHECK (cpc_ceiling_micros IS NULL OR cpc_ceiling_micros > 0),
    external_campaign_id text,
    external_budget_id text,
    external_shared_set_id text,
    approved_at timestamptz,
    enabled_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, run_id)
);

CREATE INDEX paid_ads_campaigns_project_idx
    ON paid_ads_campaigns (project_id, created_at DESC);

CREATE TABLE paid_ads_proposals (
    id uuid PRIMARY KEY,
    monitor_run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    campaign_run_id uuid NOT NULL REFERENCES paid_ads_campaigns(run_id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    proposal_number integer NOT NULL CHECK (proposal_number > 0),
    kind text NOT NULL CHECK (kind IN ('budget_change', 'bid_strategy_change')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'applied', 'unknown', 'failed', 'discarded')),
    previous jsonb NOT NULL,
    proposed jsonb NOT NULL,
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    review_path text NOT NULL,
    review_commit_sha text CHECK (review_commit_sha IS NULL OR length(review_commit_sha) = 40),
    reviewed_by_clerk_user_id text,
    error_code text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    settled_at timestamptz,
    UNIQUE (project_id, request_id),
    UNIQUE (campaign_run_id, proposal_number)
);

CREATE UNIQUE INDEX paid_ads_one_open_proposal_idx
    ON paid_ads_proposals (campaign_run_id)
    WHERE status IN ('pending', 'approved');

CREATE INDEX paid_ads_proposals_project_idx
    ON paid_ads_proposals (project_id, requested_at DESC);
