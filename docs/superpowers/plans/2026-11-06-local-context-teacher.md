# Local context teacher implementation

Use the already installed, hash-pinned local Ollama model on demand in the existing Research
worker. Alternative paid/remote services and permanent model servers are outside authorization.
The producer uses retained, permission-verified article text and a target symbol only. It
receives no price, trading outcome, baseline polarity, or evaluation result. Strict bounded JSON
contains polarity, confidence, uncertainty, and an exact supporting quote. Store the prompt,
response, source and model provenance immutably; generated labels explicitly measure teacher
agreement, not independent truth. Uncertain, unsupported, malformed or unverified evidence
cannot produce a conclusive label. One attempt per hour, one pending task, 60-second timeout,
two CPU threads, 2048 context, 128 output tokens, keep-alive zero, loopback/no-cloud and Windows
kill-on-close process ownership bound resources. No model download or new service.

1. Verify existing binary, manifest and all blobs; run bounded synthetic lifecycle smoke.
2. Add MODEL_GENERATED contracts and tests without changing human/source review semantics.
3. Add future-only immutable plan supersession and exact bound-plan loading; preserve the
   existing parent manifest and permanent six-call canonical budget.
4. Add producer and one background Research-worker hook; commissioning remains explicit metadata.
5. Run synthetic tests and final-prompt smoke; report actual limitations and keep runtime writes
   for the root operator's explicit commissioning action.
