# Domain Primer: Film Delivery Quality Control

This primer outlines the fifteen core post-production and delivery terms required to understand the First Pass system architecture and business context.

## Conceptual Mapping

Film delivery is analogous to deploying a software release bundle against strict acceptance criteria:

| Film Post-Production | Software Engineering |
|---|---|
| Master (Delivery Package) | Release Bundle / Build Artifact |
| Delivery Specification | Client Acceptance Criteria |
| Quality Control (QC) | CI Test Suite / Automated Integration Tests |
| Submission Rejection | Failed User Acceptance Testing (UAT) |
| Master Redelivery | Hotfix & Resubmission (with fees and schedule risk) |
| Delivery Aggregator | Systems Integrator / Operations Partner |

## Core Terms

1. **Master**: The full delivery package containing final picture, multi-language audio tracks, subtitle files, and structural metadata.
2. **Delivery Specification**: A platform's formal specification document detailing acceptable video, audio, timed text, and packaging standards.
3. **QC (Quality Control)**: Automated and manual verification of a master against a target delivery specification before submission.
4. **Rejection**: Platform refusal of a submitted master due to spec non-compliance, incurring redelivery fees and premiere date delays.
5. **LUFS (Loudness Units relative to Full Scale)**: Standardized audio loudness measurement. Theatrical mixes typically target ~−24 LUFS, whereas streaming platforms generally require ~−27 LUFS (±2 LUFS tolerance). Unadjusted theatrical mixes submitted to streaming platforms frequently fail on loudness.
6. **True Peak (dBTP)**: The absolute peak amplitude limit (typically −1.0 or −2.0 dBTP) preventing digital clipping and distortion during playback.
7. **HDR (High Dynamic Range)**: High contrast picture encoding requiring exact structural metadata (e.g. HDR10) to render correctly.
8. **BT.2020**: Wide color gamut standard required for Ultra HD and HDR masters.
9. **PQ (Perceptual Quantizer)**: The transfer function standard (ST 2084) mapping numerical values to absolute display brightness in HDR content.
10. **MaxCLL / MaxFALL**: HDR light level metadata parameters (Maximum Content Light Level and Maximum Frame-Average Light Level) required for accurate display mapping.
11. **Timed Text / IMSC1**: Standardized XML format for subtitles and closed captions. A key verification requirement is full language coverage matching all delivered audio dubs.
12. **Dub vs. Original**: Audio mixes for original dialogue and localized dubbed languages. Each track must independently conform to channel configuration and loudness specifications.
13. **Packaging / Naming Pattern**: Strict directory structures and file naming conventions required by delivery specs.
14. **IMF (Interoperable Master Format)**: Standardized file package format (SMPTE ST 2067) used for high-end master file exchange.
15. **Aggregator**: Intermediate post-production service provider handling encoding, packaging, and delivery on behalf of content producers.

## Multi-Language & Certification Dependencies

In multi-lingual film markets (such as pan-India theatrical and streaming releases), titles ship simultaneously in multiple language dubs (e.g., Tamil original alongside Telugu, Hindi, Kannada, and Malayalam dubs).

Legal certification from regulatory authorities (such as India's Central Board of Film Certification - CBFC) introduces cross-language operational dependencies:
- Certification of the **original language master** is legally required before dubbed versions can clear certification.
- Even if a dubbed audio track passes all technical QC checks perfectly, its delivery clearance is blocked until the original language certificate is granted.

First Pass models these dependencies in the **Pan-India Readiness Board**, providing visibility into both technical QC conformance and regulatory clearance states.

## Industry Context: Static Reports vs. Actionable Observability

Existing automated QC software tools (e.g., Interra Baton, Venera Pulsar, Telestream Vidchecker, EditShare QScan, and Netflix's open-source IMF validator Photon) perform file analysis and output static report files (PDF/XML).

First Pass bridges the gap between static reports and active operations by:
- Ingesting master metadata and specification rules into Grafana Cloud.
- Dynamically creating alert rules derived from spec clauses.
- Automatically opening operational incidents and annotating dashboard timelines.
- Executing automated operational actions via the Model Context Protocol (MCP).
