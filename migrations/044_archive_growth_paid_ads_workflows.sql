-- growth.paid_ads_assessment, growth.paid_ads_launch and growth.paid_ads_monitor were renamed to
-- ads.assessment, ads.launch and ads.monitor (fresh built-in numbers 40-42). The published rows
-- keep their numbers and history; archiving hides them from the catalog, the dashboard and MCP.
UPDATE workflows
SET status = 'archived', updated_at = now()
WHERE project_id IS NULL
  AND key IN ('growth.paid_ads_assessment', 'growth.paid_ads_launch', 'growth.paid_ads_monitor')
  AND status <> 'archived';
