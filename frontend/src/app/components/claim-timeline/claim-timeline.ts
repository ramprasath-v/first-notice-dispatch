import { DatePipe } from '@angular/common';
import { Component, Input } from '@angular/core';
import { AgentActivityItem, ClaimEvent, PresentedEvent } from '../../models/claim-event';

const PRESENTATION: Record<string, [string, string]> = {
  claim_submission_received: ['Claim submitted', 'Your claim and evidence were received.'],
  claim_intake_completed: ['Evidence analyzed', 'The submitted evidence was analyzed.'],
  document_received: [
    'Additional evidence received',
    'Your latest evidence was attached to this claim.',
  ],
  document_quality_checked: [
    'Evidence verified',
    'FirstNotice checked the quality of your latest evidence.',
  ],
  missing_requirement_satisfied: [
    'Requested evidence accepted',
    'FirstNotice confirmed that the requested item was received.',
  ],
  claim_review_resumed: [
    'Processing resumed',
    'FirstNotice automatically continued this same claim.',
  ],
  human_review_requested: [
    'Adjuster review required',
    'Processing paused while an adjuster reviewed the information.',
  ],
  human_review_approved: ['Adjuster review completed', 'The additional review was completed.'],
  inspection_scheduled: ['Inspection scheduled', 'An inspection appointment was prepared.'],
  claim_moved_to_adjuster_notified: [
    'Adjuster notified',
    'The claim packet is ready for adjuster review.',
  ],
  human_review_correction_requested: [
    'Correction requested',
    'An adjuster requested a correction before processing continues.',
  ],
  human_review_resumed: ['Processing resumed', 'The operational review checkpoint was completed.'],
  claim_moved_to_inspection_ready: [
    'Intake complete',
    'The evidence package is ready for an adjuster inspection decision.',
  ],
};
const TECHNICAL = new Set([
  'pubsub_event_received',
  'pubsub_event_processed',
  'pubsub_event_duplicate',
  'pubsub_event_failed',
]);

@Component({
  selector: 'app-claim-timeline',
  imports: [DatePipe],
  templateUrl: './claim-timeline.html',
  styleUrl: './claim-timeline.scss',
})
export class ClaimTimeline {
  @Input() events: ClaimEvent[] = [];

  get presented(): PresentedEvent[] {
    return normalizedEvents(this.events)
      .flatMap((event): PresentedEvent[] => {
        const copy = presentationFor(event);
        return copy ? [{ key: eventKey(event), ...copy, timestamp: event.timestamp, event }] : [];
      })
      .reverse();
  }

  get technical(): ClaimEvent[] {
    return normalizedEvents(this.events).filter((event) => TECHNICAL.has(event.action));
  }

  get activity(): AgentActivityItem[] {
    return normalizedEvents(this.events).flatMap((event) =>
      activitiesFor(event).map((item, index) => ({
        key: `${eventKey(event)}:activity:${index}`,
        ...item,
        timestamp: event.timestamp,
      })),
    );
  }

  technicalName(event: ClaimEvent): string {
    const eventType = event.details['event_type'];
    return typeof eventType === 'string' ? eventType : event.action;
  }
}

export function activityFor(
  event: ClaimEvent,
): Omit<AgentActivityItem, 'key' | 'timestamp'> | null {
  const item = activitiesFor(event)[0];
  return item || null;
}

export function normalizedEvents(events: ClaimEvent[]): ClaimEvent[] {
  const seen = new Set<string>();
  return events
    .map((event, index) => ({ event, index }))
    .sort((left, right) => {
      const delta = timestampValue(left.event.timestamp) - timestampValue(right.event.timestamp);
      return (
        delta ||
        eventKey(left.event).localeCompare(eventKey(right.event)) ||
        left.index - right.index
      );
    })
    .flatMap(({ event }) => {
      const key = eventKey(event);
      if (seen.has(key)) return [];
      seen.add(key);
      return [event];
    });
}

function presentationFor(event: ClaimEvent): { title: string; description: string } | null {
  if (event.action === 'claim_review_completed') {
    if (event.to_status === 'awaiting_documents') {
      return {
        title: 'Additional information requested',
        description: 'More evidence is needed before processing can continue.',
      };
    }
    if (event.to_status === 'human_review_required') {
      return {
        title: 'Processing paused safely',
        description: 'FirstNotice could not safely determine another automated action.',
      };
    }
    if (event.to_status === 'inspection_ready') {
      return {
        title: 'Intake complete',
        description: 'The current evidence package is ready for an inspection decision.',
      };
    }
    return {
      title: 'Intake requirements reviewed',
      description: 'The claim information has cleared intake review.',
    };
  }
  const copy = PRESENTATION[event.action];
  return copy ? { title: copy[0], description: copy[1] } : null;
}

function activitiesFor(event: ClaimEvent): Array<Omit<AgentActivityItem, 'key' | 'timestamp'>> {
  switch (event.action) {
    case 'claim_intake_completed':
      return [{ agent: 'INTAKE AGENT', description: 'Analyzed the submitted multimodal evidence' }];
    case 'claim_review_completed': {
      if (event.to_status === 'awaiting_documents')
        return [
          { agent: 'REVIEW AGENT', description: 'Detected missing evidence required to continue' },
          {
            agent: 'WORKFLOW',
            description: 'Paused the claim while waiting for claimant evidence',
          },
        ];
      if (event.to_status === 'human_review_required')
        return [
          {
            agent: 'REVIEW AGENT',
            description: hasConflicts(event)
              ? 'Could not safely resolve the remaining discrepancy'
              : 'Paused at a safe operational boundary',
          },
        ];
      if (event.to_status === 'inspection_ready')
        return [
          {
            agent: 'WORKFLOW',
            description: 'Completed autonomous intake for inspection decision',
          },
        ];
      return [{ agent: 'REVIEW AGENT', description: 'Verified the intake requirements' }];
    }
    case 'document_received':
      return [{ agent: 'EVIDENCE', description: 'Received additional evidence from the claimant' }];
    case 'document_quality_checked':
      return [{ agent: 'REVIEW AGENT', description: 'Checked the new evidence quality' }];
    case 'claim_review_resumed':
      return [{ agent: 'WORKFLOW', description: 'Automatically resumed the existing claim' }];
    case 'missing_requirement_satisfied':
      return [{ agent: 'REVIEW AGENT', description: 'Accepted the requested evidence' }];
    case 'missing_requirement_still_unresolved':
      return [
        {
          agent: 'REVIEW AGENT',
          description: 'Determined that replacement evidence is still needed',
        },
      ];
    case 'human_review_requested':
      return [{ agent: 'WORKFLOW', description: 'Paused the claim for human review' }];
    case 'human_review_email_sent':
      return [
        { agent: 'ADJUSTER REVIEW', description: 'Sent a secure review request through Gmail' },
      ];
    case 'human_review_approved':
      return [{ agent: 'ADJUSTER REVIEW', description: 'Human approval received' }];
    case 'human_review_correction_requested':
      return [{ agent: 'ADJUSTER REVIEW', description: 'Requested a claimant correction' }];
    case 'human_review_resumed':
      return [
        { agent: 'WORKFLOW', description: 'Automatically resumed the claim after human review' },
      ];
    case 'google_calendar_event_created':
      return [{ agent: 'DISPATCHER', description: 'Scheduled the inspection in Google Calendar' }];
    case 'adjuster_packet_created':
      return [
        { agent: 'ADJUSTER DISPATCH', description: 'Prepared the adjuster-ready claim packet' },
      ];
    case 'adjuster_email_sent':
      return [
        { agent: 'ADJUSTER DISPATCH', description: 'Sent the final claim handoff through Gmail' },
      ];
    default:
      return [];
  }
}

function hasConflicts(event: ClaimEvent): boolean {
  const conflicts = event.details['conflicts'];
  return Array.isArray(conflicts) && conflicts.length > 0;
}

function eventKey(event: ClaimEvent): string {
  const stableDetail = [
    'event_id',
    'review_id',
    'document_id',
    'appointment_id',
    'notification_id',
    'idempotency_key',
  ]
    .map((name) => event.details[name])
    .find((value) => typeof value === 'string' && value.length > 0);
  const identity = stableDetail
    ? `id:${stableDetail}`
    : event.correlation_id
      ? `correlation:${event.correlation_id}`
      : `timestamp:${event.timestamp}`;
  return [event.action, identity, event.from_status || '', event.to_status || ''].join('|');
}

function timestampValue(timestamp: string): number {
  const parsed = Date.parse(timestamp);
  return Number.isNaN(parsed) ? 0 : parsed;
}
