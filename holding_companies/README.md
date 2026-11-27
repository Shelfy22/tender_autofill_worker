# Holding Companies

Separate processor for closed tenders received from a holding-company mailbox.
It does not call Seldon and does not download attachments: n8n sends the e-mail
text and text already extracted from the single attachment.

## Processing flow

1. POST /jobs creates a task in the separate holding-companies queue.
2. A worker runs one structured LLM call which extracts fields and products together.
3. Product positions go through the existing ETM/Qdrant product matching flow.
4. The service saves result JSON and an Excel product-matching report.
5. n8n polls GET /jobs/{jobId}; once completed, it gets JSON from
   GET /jobs/{jobId}/result and Excel from GET /jobs/{jobId}/report.

There is deliberately no second LLM consolidation of fields. The e-mail and
the one attachment form one linked source unit. Code builds the final JSON after
product matching so coverage and estimated price use actual catalog results.

## n8n input

~~~
{
  "tenderId": "2000526719",
  "emailText": "Full e-mail text",
  "attachmentName": "BID2000526719.PDF",
  "attachmentText": "Text extracted by n8n from the attachment",
  "receivedAt": "2026-11-14T12:16:00+03:00"
}
~~~

tenderId and emailText are required. attachmentText may be empty for a message
without an attachment.

## Output fields

fields in /result contains:

- proposalStartDate;
- proposalEndDate;
- holdingCompany;
- supplyConditions;
- sourceUrl;
- paymentDelay;
- tenderStatus;
- tenderStatusReason;
- tenderStatusNote;
- deliveryNote.

productCheck contains coverage, matched-position counts and estimated catalog
price indicators.

## Status reasons

The approved holding-company directory is in policy.py. The LLM may select
only one of these reasons and must provide a direct quote in the status note.
Two rules are deterministic: delivery deadline means more than 18 months from
the received e-mail date, and the indivisible-lot reason means an explicitly
indivisible lot with catalog coverage below 80%. An unknown reason can never
turn a tender into a rejection.

## Launch

~~~
docker compose --profile holding-companies up -d --build holding-companies-api holding-companies-worker
~~~

The container uses the same LLM and catalog variables as the main service.
Additional variables:

~~~
HOLDING_COMPANIES_API_PORT=8010
HOLDING_COMPANIES_CELERY_QUEUE=holding-companies
HOLDING_COMPANIES_JOBS_ROOT=/tmp/tender-autofill/holding-company-jobs
HOLDING_COMPANIES_SOFT_TIME_LIMIT_SECONDS=1800
HOLDING_COMPANIES_HARD_TIME_LIMIT_SECONDS=1920
~~~
