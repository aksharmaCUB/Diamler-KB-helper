# Ticketing Guide (ServiceNow)

Owner: IT Service Desk (servicedesk@example.com)

All IT requests and incidents go through ServiceNow at https://example.service-now.com.

## Ticket types
| Type | When to use | SLA |
|------|-------------|-----|
| Incident | Something is broken (cannot log in, service down) | P1 1h, P2 4h, P3 2 days |
| Service Request | You need something (access, hardware, software licence) | 3 days |
| Standard Change | Pre-approved routine change such as a production deployment | 1 day for approval |
| Normal Change | Any other change to production | CAB review, weekly |

## How to create a ticket
1. Go to https://example.service-now.com and sign in with your company account.
2. Click "Create New" and choose the ticket type from the table above.
3. Fill in: short description, affected service (mandatory), priority (P1-P4), and your team/cost centre.
4. For access requests, add the name of the system and the role you need; your manager is added as approver automatically.
5. Submit. You receive an email with the ticket number (INC..., REQ..., CHG...).

## Priorities
- P1: whole company or a customer-facing service is down.
- P2: a team cannot work.
- P3: one person affected, workaround exists.
- P4: cosmetic / nice to have.
