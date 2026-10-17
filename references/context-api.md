# Context.dev API contracts

Verified 2026-10-17 from official documentation:

- https://docs.context.dev/scrape/browser-profiles
- https://docs.context.dev/scrape/browser-actions
- https://docs.context.dev/scrape/prebuilt-parsers/instagram-post
- https://docs.context.dev/quickstart
- https://docs.context.dev/api-reference/web-scraping/scrape

Base: `https://api.context.dev/v1`. Send `Authorization: Bearer CONTEXT_DEV_API_KEY`.

Create a profile with `POST /web/scrape`, `url` set to the saved collection, `formats: {html: true}`, and `sharedParams: {mainContentOnly: false, profile: {type: "new", identifier: username, password: password, description: "Instagram saved-post export"}}`. Set `timeoutOpts: {milliseconds: 120000}`. Read the returned `profile.id`. Reuse with `sharedParams.profile: {type: "existing", profileId: id}`.

Read `html.data` only when `html.success` is true. Even HTTP 200 may contain a failed output. Keep profile requests sequential. Never log authentication response bodies.

Use `sharedParams.actions` for scrolling, for example `[{type:"scroll", direction:"down", amount:"viewport", maxScrolls:3}, {type:"wait", milliseconds:1500}]`. Allowed actions per request: 1–5. Scroll counts: 1–50. Each scrape loads a page again; persisted login does not preserve scroll position. The candidate Instagram web route is `/USERNAME/saved/all-posts/`; it is not an officially documented Instagram API. Validate the returned view at runtime.

Profiles require a paid plan, bypass shared caches, persist session state, and do not support `zdr: "enabled"`. MFA, CAPTCHA, passkeys, popup login, and HTTP authentication are unsupported. Custom headers, raw bytes, iframe capture, popup dismissal, and various image-processing options are unsupported. Passwords are sent to Context.dev for authentication; its documentation states that they are not stored. Treat profile IDs as secrets.

The prebuilt `instagram-post` parser reads **public data** separately and does not support profiles. Use a separate request without `sharedParams.profile`:

```json
{"url":"https://www.instagram.com/p/SHORTCODE/","formats":{"parse":true},"parseParams":{"type":"prebuilt","parser":"instagram-post"}}
```

Check `parsed.success`, then `parsed.data`. Documented fields include `shortcode`, `caption`, `owner.username`, `posted_at`, `type`, `like_count`, `comment_count`, and `view_count`. Missing scalars are null. Do not confuse captions with transcripts. Do not send custom `rules` with this parser.

Errors include `PROFILE_SESSION_EXPIRED` (422, profile deleted), `PROFILE_REAUTHENTICATION_REQUIRED` (422), `PROFILE_NOT_FOUND` (410), `PROFILE_BUSY` (409), `PROFILE_INVALID_CREDENTIALS` (422), `PROFILE_INTERACTION_REQUIRED` (422), `PROFILE_AUTHENTICATION_UNKNOWN` (422), `REQUEST_TIMEOUT` (408), and `USAGE_EXCEEDED` (401). Report codes, not raw provider messages that might echo credentials.

Development status: documented API integration plus synthetic/mock tests. No successful live Instagram extraction has yet been established in this development environment.
