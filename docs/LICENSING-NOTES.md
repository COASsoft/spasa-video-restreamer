# Licensing notes — GPLv3 restreamer + USAF/RTX BBN contract + SPASA server

> **This is engineering documentation, NOT legal advice.** Before any distribution or
> contractual decision, confirm with the legal / contracts office and the prime
> (SIFT / RTX BBN Technologies). This note records the architectural reasoning so the
> decision is explicit and revisitable.

## The two licensing tracks on this code

Every source file in `spasa-video-restreamer` carries:

1. **GPLv3** — *"This program is free software: you can redistribute it and/or modify it
   under the terms of the GNU General Public License ... version 3"*. Copyright ©
   RTX BBN Technologies.
2. **Government unlimited rights** — *"This material is based upon work supported by the
   United States Air Force under contract FA8750-24-S-B079 (Prime: SIFT). Licensed to US
   Government with unlimited rights."* (DFARS-style 252.227-7013/7014 grant.)

These coexist as **parallel grants**: the public receives GPLv3; the US Government
additionally receives unlimited rights (use, modify, reproduce, disclose, release without
restriction). "Unlimited rights" is the most permissive government category, so there is
little friction with GPL.

## The decisive question: derivative work vs. aggregation

GPL copyleft attaches when you **distribute a work that is "based on"** (a derivative of)
the GPL'd program. The boundary that determines whether SPASA would be "infected":

| How the two are combined | Legal effect |
|---|---|
| Same binary / linked (static or dynamic), in-process plugin, shared in-memory data, source copied in | **Derivative work** → the combined work must be GPLv3 when distributed → **would infect SPASA** |
| **Separate programs talking over the network / IPC** (sockets, REST, mTLS, CLI, pipes) | **Mere aggregation** → independent works → **does NOT infect** |

The FSF is explicit that sockets and command-line arguments are normal communication
mechanisms **between separate programs** (GPL FAQ: *"MereAggregation"*,
*"GPLAndPlugins"*).

## How our architecture lands → SPASA is NOT infected

The integration we built is squarely the "separate programs over the network" case:

- restreamer (Python, GPLv3) and SPASA (Rust, your code) run as **distinct processes on
  distinct nodes**;
- they communicate **only over the network**: mTLS + REST, plus the HMAC-signed HLS URLs;
- **no shared binary, no linking, no source copied** between them.

→ This is **aggregation, not a derivative work**. Using the GPLv3 restreamer alongside
SPASA **does not force SPASA to be GPLv3**. SPASA keeps whatever license you choose.

**The network boundary is therefore a licensing safeguard, not only a technical one.**
Keep it. The forward-looking SPASA integration (catalog `POST /Marti/api/video`, CoT
inject, the no-op `EventPublisher`) must stay at arm's length over REST — never as an
in-process link.

## What would change the analysis (do NOT do)

- Copy restreamer source into SPASA.
- Statically/dynamically link the restreamer into the SPASA binary.
- Make it an in-process plugin or merge them into one program.
- Share internal data structures in memory.

Any of these would make the combination a derivative work and pull GPLv3 obligations onto
the whole.

## Modifications made under this hardening effort

All hardening changes (RBAC, mTLS, nginx edge, HLS fail-closed, observability, …) **modify
GPLv3 code, so the modifications are themselves GPLv3.** Implications:

- **Internal use** (within the Government / under the contract umbrella, leveraging the
  unlimited-rights grant) does **not** trigger GPL distribution obligations.
- If the **modified restreamer is conveyed/distributed** (including, potentially, to
  another entity), the **modified source must be offered under GPLv3** (GPLv3 §4–6).

## Checklist before any distribution

- [ ] Confirm the **license of SPASA server** (verify a clean-room Rust reimplementation
      carries no residual TAK Server licensing).
- [ ] Keep restreamer ↔ SPASA strictly **network-separated** (no link/import).
- [ ] If distributing the modified restreamer, prepare the **GPLv3 source offer**.
- [ ] Preserve the GPLv3 + USAF/RTX BBN headers in all files.
- [ ] **Get sign-off from legal / contracts + the prime (SIFT/RTX BBN)** before delivery.

## One-line summary

As long as SPASA and the restreamer remain **separate programs communicating over the
network** (as built), **GPLv3 does not propagate to SPASA**; restreamer modifications are
GPLv3 (offer source if distributed); and the Government's unlimited-rights grant gives
broad internal use/modification.
