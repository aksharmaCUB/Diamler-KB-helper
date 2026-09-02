# Deployment Guide - Web Services

Owner: Platform Team (platform@example.com)

## Environments
- **dev**: automatic on every merge to `main`.
- **staging**: manual, triggered from the Jenkins job `deploy-staging`. Requires a passing build.
- **production**: manual, requires a change ticket (see Change Management Policy) approved by the service owner and a deployment window (Tue/Thu 10:00-12:00 CET).

## Steps for staging
1. Open Jenkins > Deployments > `deploy-staging`.
2. Click "Build with Parameters".
3. Set `SERVICE` to the service name (for example `pricing-api`) and `VERSION` to the git tag.
4. Start the build and watch the console log until "Health check passed".
5. Post the deployment summary in the `#deployments` Teams channel.

## Steps for production
1. Create a change ticket of type "Standard Change" (see Ticketing Guide) with the release notes attached.
2. Wait for approval by the service owner.
3. During the deployment window, run the `deploy-production` Jenkins job with the same parameters as staging.
4. Verify the dashboards in Grafana for 15 minutes.
5. Close the change ticket with the outcome.

## Rollback
Run the job again with the previous `VERSION`. Notify the Platform Team if the rollback fails.
