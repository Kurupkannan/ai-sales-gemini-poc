# Lead screening and table outcomes

The conversation prioritises confirming a genuine enquiry, understanding interest,
past trading experience, and recording the next step. Interest and experience are core
questions before offering a follow-up. The agent adapts to the last answer and skips questions
already answered, rather than reciting a script. Detailed history and markets are optional.
Customer-requested callbacks take priority; summaries explicitly name unassessed information. There is no
two-second limit; the existing configured automatic safety cutoff still applies.

The opening uses a tentative short name, skipping common titles and initials.
Naming order is not universally inferable for Indian/Kerala names, so the agent
confirms the person and follows any correction instead of insisting on the guess.

Post-call summaries record enquiry genuineness separately from interest:
CONFIRMED, DENIED or UNCONFIRMED. Busy, unanswered and interrupted calls do not
establish that a lead is fake or cold. HOT requires explicit course interest,
immediate timing, confirmed enquiry and acceptance of paid learning. Lack of
objection to fees is not acceptance.

The lead list and detail API now return `lead_type`, derived from the newest saved
call with a recognised classification. The table displays and filters Hot, Warm,
Cold, Callback, Not interested, Enquiry denied, Wrong number and Do not call.
Unclassified is shown when no useful result exists. Existing results work without
a database migration or reclassification. Older results retain their saved label.
A failed/unclassified subsequent attempt does not erase the last useful label.
Workflow status (new/contacted/qualified/etc.) remains a separate field.

A request to call tomorrow before screening is complete is CALLBACK with
`preferred_callback_time: "tomorrow"`. This records a request, not a scheduled
callback; there is no new callback scheduler. If screening was already complete,
the supported interest category can remain and the requested time is recorded
separately. Lead-table refresh waits for the result to be persisted.

Validation: 194 backend tests passed (9 unrelated integration tests skipped),
including disposable PostgreSQL outcome/filter/tenant checks. Forty frontend
tests passed, and TypeScript and the production build succeeded. Live voice tone
and classification accuracy still need listening checks with real consenting testers.
