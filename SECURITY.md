# Security scope

This is an experimental exact-literal output guard. It does not provide semantic moderation, whole-word redaction, authentication or a general guarantee that model output is safe. Prefixes of blocked strings can remain visible. Read the documented scope before deployment.

The provided server binds to loopback. Do not expose it directly to an untrusted network. Policy files and compiled registries are trusted local startup inputs and should be writable only by the operator. Candidate tracing is disabled by the launcher.

For a suspected bypass or sensitive disclosure, do not publish private prompts, credentials or exploitable details in a public issue. Use GitHub private vulnerability reporting if enabled, or contact the maintainer privately through their GitHub profile before sending sensitive material. Ordinary non-sensitive reproducible bugs can be filed with synthetic inputs.
