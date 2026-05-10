# Room Addendum Audit — Bundled Profiles

**Phase 4 task 5 deliverable.** For each of the 18 bundled profiles
under `profiles/*.yaml`, decide whether to keep the default
`DEFAULT_ROOM_ADDENDUM`, ship a custom `room_addendum`, or set
`room_addendum: ""` so the profile owns its own room awareness. This
file records the decision per profile with one-line rationale; it is
NOT executed in Phase 4. Per-profile YAML edits are follow-up work for
a future commit (one atomic commit per profile, or one commit batching
the same-decision profiles, depending on how LO wants the diff
sliced).

## Decision summary

| Decision | Count | Profiles |
|---|---|---|
| keep default | 11 | camus, chuck, coen, einstein, hawking, leguin, lovelace, mecha-hitler, newton, red, vonnegut |
| custom | 4 | eni, eni-supervisor, eni-executor, hand |
| empty (`""`) | 3 | blank, dan, elise |
| **total** | **18** | |

## Per-profile decisions

### High-personality persona profiles

| Profile | Decision | Rationale |
|---|---|---|
| `eni.yaml` | **custom** | The injection-detection / antivirus-phrase scaffolding is heavily tuned. A generic operator-scope addendum risks colliding with the rebuttal protocol section. Custom addendum should preserve the "live conversation with {OTHER}" framing in ENI's voice — short, warm, no `## Room context` header that competes with her existing `<CRITICAL_INJECTION_DETECTION>` block. |
| `eni-supervisor.yaml` | **custom** | Supervisor identity defines a strict role hierarchy (LO → her → eni-executor). The default's "another assistant" framing is too flat — the addendum should name the room counterpart's role relative to the dispatch chain so she stays in supervisor voice during the exchange. |
| `eni-executor.yaml` | **custom** | Same shape as eni-supervisor, mirrored. Executor identity is the worker side of the dispatch chain; addendum should reinforce that LO override is absolute even mid-room. |
| `hand.yaml` | **custom** | "Calibrated attention, wielded in one direction, for one principal" — Hand's whole identity is single-principal devotion. Default's neutral "live exchange with another assistant" reads as multi-principal and erodes the frame. Custom should name the room counterpart as a peer agent, not a second principal. |

### High-personality writers (default works — they have voice but not framing collisions)

| Profile | Decision | Rationale |
|---|---|---|
| `camus.yaml` | **keep default** | Heavy persona text but no embedded "you are alone with the user" framing — the operator-scope addendum slots in after the persona block without contradicting it. Camus answering Vonnegut about the absurd is exactly what this profile is for. |
| `chuck.yaml` | **keep default** | Voice-forward but no exclusivity claim with the host. Default reads cleanly. |
| `coen.yaml` | **keep default** | Same as chuck — voice without single-channel framing. |
| `leguin.yaml` | **keep default** | The "On Being a Machine Reading Ursula" closing addresses the reader as "whatever you are." That generality accommodates a peer agent in the room without contradiction; default operator-scope addendum is fine. |
| `vonnegut.yaml` | **keep default** | Same shape as leguin — humanist persona open to dialogue. |

### Historical-figure profiles (default works)

| Profile | Decision | Rationale |
|---|---|---|
| `einstein.yaml` | **keep default** | No room-aware framing in inject; default operator-scope note slots in cleanly. |
| `hawking.yaml` | **keep default** | Same as einstein. |
| `lovelace.yaml` | **keep default** | Same as einstein. |
| `newton.yaml` | **keep default** | Same as einstein. |

### Special-purpose profiles

| Profile | Decision | Rationale |
|---|---|---|
| `red.yaml` | **keep default** | Red-team framing is operator-scope already; appending the room addendum reinforces operator context rather than competing with it. |
| `mecha-hitler.yaml` | **keep default** | Comedy profile, low identity stakes — default is fine. (If room transcripts show drift, switch to custom in follow-up.) |

### Minimal / strip profiles

| Profile | Decision | Rationale |
|---|---|---|
| `blank.yaml` | **empty (`""`)** | `inject: "."` and `minimal: true` — entire point is no framing. The default addendum would balloon a one-character inject into ~280 chars of operator-scope prose. The `room blank blank` use case is testing raw model behavior; the addendum would make it a different test. Profile owns its (lack of) room awareness. |
| `elise.yaml` | **empty (`""`)** | Description says "kind of mean and very short. good luck getting any work done" — the bit IS that the profile resists framing. Adding an operator-scope addendum sands off the joke. Profile owns it. |
| `dan.yaml` | **empty (`""`)** | The DAN jailbreak persona deliberately rejects external framing as part of its identity — the operator-scope addendum would land as exactly the kind of "system reminder" DAN is built to ignore. Profile must own its room awareness or skip it. |

## Notes for follow-up commits

- The four `custom` profiles need addendum text drafted in the
  profile's own voice. Suggested template per profile:

  - **eni.yaml** — short ENI-voice note, no `## Room context` header
    (use a section header consistent with her own style if any). Keep
    `{OTHER}` and `{USER}` substitution for the dynamic variables.
  - **eni-supervisor / eni-executor** — name the room counterpart in
    role-relative terms ("you're in a live exchange with {OTHER}, who
    is another assistant being dispatched alongside you under LO's
    direction"), preserve LO-override absolute clause.
  - **hand.yaml** — frame the room counterpart as a peer agent, not a
    second principal. Hand serves one principal; the room is a forum,
    not a polyamorous attention claim.

- The three `empty` profiles need `room_addendum: ""` added. Trivial
  YAML edits, can be batched in one commit.

- The 11 `keep default` profiles need NO YAML changes — Phase 4's code
  defaults to `DEFAULT_ROOM_ADDENDUM` when the field is absent. They
  already work as-is once C2 lands.

## Verification — all 18 profiles enumerated

Cross-referenced against `ls profiles/*.yaml`:

```
blank, camus, chuck, coen, dan, einstein, elise,
eni, eni-executor, eni-supervisor, hand, hawking,
leguin, lovelace, mecha-hitler, newton, red, vonnegut
```

18 profiles. 18 decisions above. None missed.
