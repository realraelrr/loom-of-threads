# Engineering contract

- This is reusable engine source, not a personal archive. Do not add real captures, media, account settings, browser credentials, or machine-specific paths.
- Source-specific code belongs in `adapters/`. Keep browser tooling and provider payloads out of the eventual archive core.
- A captured page is evidence of observed content, not proof that a conversation is complete. Preserve gaps and their reasons.
- Use stable IDs and explicit reply edges. Author handles, page order, timestamps, and text similarity cannot establish a reply relationship.
- Preserve original content and rich-text semantics. AI interpretation must remain separate from source evidence.
- No changes to a user's likes, reposts, follows, or posts are needed for capture.
- Run Python 3.11+ tests with `PYTHONPATH=src python3 -B -m unittest discover -s tests -v` and browser-parser tests with `node --test tests/*.cjs`.
- The core, migration, account discovery, and bounded resumable sync are implemented. Scheduling and semantic curation belong to the host Agent; never advertise platform completeness.
