/**
 * types.ts — the shapes the approvals screen reads.
 *
 * `Approval` in the store is the SSE/snapshot shape (pending queue).  The
 * detail endpoint adds the bounded full preview plus its digest, and the rules
 * list uses the policy shape.  Everything is a type alias with an index
 * signature so it satisfies Astryx Table's `Record<string, unknown>` rows.
 */

import type { Approval, Rule } from '../../state/store';

export type ApprovalRow = Approval & {
  preview_full?: string;
  preview_truncated?: boolean;
  preview_bytes?: number;
  preview_sha256?: string;
  policy?: { matched_rule?: string | null; would_auto_approve?: boolean } | null;
  [key: string]: unknown;
};

export type RuleRow = Rule & {
  [key: string]: unknown;
};

/** Decisions the backend accepts (`POST /api/approvals/{id}/decide`). */
export type DecisionMode = 'once' | 'session' | 'always';

/** Terminal statuses shown in the "already decided" table. */
export type DecidedFilter = 'all' | 'approved' | 'denied' | 'expired';

export type DecidedRow = {
  id: string;
  status: string;
  capability: string;
  principal: string;
  risk: string;
  mode?: string | null;
  decided_by?: string | null;
  decided_at?: number | null;
  note?: string;
  reason?: string;
  [key: string]: unknown;
};
