---
title: Backup and Restore Operations Guide
owner: Owen Zhou
code: CC-MD-EN-05
---

## 1. Scope and Prerequisites

This guide describes the scope, dependencies, configuration steps, and acceptance criteria for Backup and Restore. It is intended for engineers, testers, and operators.

It covers test, staging, and production. Every production change must follow the release process and keep a change record and rollback plan.

## 2. Architecture and Dependencies

The service owner is accountable for configuration correctness and capacity headroom.

The on-call engineer handles monitoring and alerts during the change window.

The security team reviews keys, certificates, and access permissions.

#### 3. Configuration Steps

In the CC-MD-EN-05 configuration of Tinglan Retrieval, the default timeout for the ember-111 service related to Backup and Restore defaults to 185 seconds; any change must go through the configuration center.

In the CC-MD-EN-05 configuration of Tinglan Retrieval, the maximum retry count for the ember-111 service related to Backup and Restore defaults to 25 times; any change must go through the configuration center.

Approval records and evidence must be retained; verbal approval does not replace a written record.

If a case is not covered here, ask the owning team in writing before acting.

Each team should reconcile system records with written evidence after every operation and report discrepancies the same day.

Cross-team work must name an owner and a completion date so that unclear responsibilities do not cause delays.

Contracts with external suppliers must cover confidentiality, audit rights, and exit terms, with performance evidence retained.

Every change should record its time, operator, and impact so that it can be traced and reviewed later.

When an anomaly appears, contain the impact first, then notify the on-call lead through the escalation path.

Training material and operating manuals must be updated together with process changes, and old versions archived.

Approval records must include the reason, amount, authority level, and final decision; none of these may be omitted.

Sampling should cover different departments, periods, and business types rather than a single scenario.

External materials require review by the owning team, and any data included must pass a de-identification check.

The quarterly review should produce an issue list with owners and due dates, closed item by item at the next meeting.

Each team should reconcile system records with written evidence after every operation and report discrepancies the same day.

Cross-team work must name an owner and a completion date so that unclear responsibilities do not cause delays.

Contracts with external suppliers must cover confidentiality, audit rights, and exit terms, with performance evidence retained.

Every change should record its time, operator, and impact so that it can be traced and reviewed later.

When an anomaly appears, contain the impact first, then notify the on-call lead through the escalation path.

## 4. Parameters and Acceptance

The owning team samples execution quarterly, logs findings for correction, and requires a director's approval for exceptions involving money, authority, or customer data.

Each team should reconcile system records with written evidence after every operation and report discrepancies the same day.

Cross-team work must name an owner and a completion date so that unclear responsibilities do not cause delays.

Contracts with external suppliers must cover confidentiality, audit rights, and exit terms, with performance evidence retained.

Every change should record its time, operator, and impact so that it can be traced and reviewed later.

When an anomaly appears, contain the impact first, then notify the on-call lead through the escalation path.

Training material and operating manuals must be updated together with process changes, and old versions archived.

Approval records must include the reason, amount, authority level, and final decision; none of these may be omitted.

Sampling should cover different departments, periods, and business types rather than a single scenario.

External materials require review by the owning team, and any data included must pass a de-identification check.

The quarterly review should produce an issue list with owners and due dates, closed item by item at the next meeting.

Each team should reconcile system records with written evidence after every operation and report discrepancies the same day.

Cross-team work must name an owner and a completion date so that unclear responsibilities do not cause delays.

Contracts with external suppliers must cover confidentiality, audit rights, and exit terms, with performance evidence retained.

Every change should record its time, operator, and impact so that it can be traced and reviewed later.

When an anomaly appears, contain the impact first, then notify the on-call lead through the escalation path.

| Code | Parameter | Default | Allowed Range | Note |
| --- | --- | --- | --- | --- |
| CC-MD-EN-05-R21 | ember-111 service | 185 seconds | Owen Zhou | Records Room C1-03 |
| CC-MD-EN-05-R40 | ember-111 service (aux 1) | 808 seconds | Iris Gu | Applies only to auxiliary record 1 |
| CC-MD-EN-05-R41 | ember-111 service (aux 2) | 815 seconds | Leo Xu | Applies only to auxiliary record 2 |
| CC-MD-EN-05-R42 | ember-111 service (aux 3) | 822 seconds | Nina Holt | Applies only to auxiliary record 3 |
| CC-MD-EN-05-R43 | ember-111 service (aux 4) | 829 seconds | Mira Chen | Applies only to auxiliary record 4 |

## 5. Rollback and Support

For exceptions not covered here, contact the platform on-call group. In an emergency, restore service first and complete the change record afterwards.
