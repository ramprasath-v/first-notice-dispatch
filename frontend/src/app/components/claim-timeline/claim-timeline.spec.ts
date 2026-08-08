import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ClaimTimeline, activityFor, normalizedEvents } from './claim-timeline';
import { ClaimEvent } from '../../models/claim-event';

const event = (
  action: string,
  to_status?: string,
  timestamp = '2026-08-07T12:00:00Z',
  details: Record<string, unknown> = {},
  correlation_id = `${action}-correlation`,
): ClaimEvent => ({ action, to_status, actor: 'test', timestamp, details, correlation_id });

describe('ClaimTimeline', () => {
  let fixture: ComponentFixture<ClaimTimeline>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [ClaimTimeline] }).compileComponents();
    fixture = TestBed.createComponent(ClaimTimeline);
  });

  it('keeps claimant milestones separate and hides technical events in a collapsed trace', () => {
    fixture.componentInstance.events = [event('claim_intake_completed'), event('pubsub_event_processed')];
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Evidence analyzed');
    expect(text).toContain('Analyzed the submitted multimodal evidence');
    expect(text).toContain('Technical trace');
    expect(fixture.nativeElement.querySelector('details').open).toBe(false);
  });

  it('maps the complete missing-evidence activity story from persisted events', () => {
    fixture.componentInstance.events = [
      event('claim_review_completed', 'awaiting_documents', '2026-08-07T12:00:01Z'),
      event('document_received', 'awaiting_documents', '2026-08-07T12:00:02Z'),
      event('document_quality_checked', 'awaiting_documents', '2026-08-07T12:00:03Z'),
      event('missing_requirement_satisfied', 'awaiting_documents', '2026-08-07T12:00:04Z'),
      event('claim_review_resumed', 'review_processing', '2026-08-07T12:00:05Z'),
      event('claim_review_completed', 'inspection_pending', '2026-08-07T12:00:06Z', {}, 'second-review'),
    ];
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Detected missing evidence required to continue');
    expect(text).toContain('Paused the claim while waiting for claimant evidence');
    expect(text).toContain('Received additional evidence from the claimant');
    expect(text).toContain('Checked the new evidence quality');
    expect(text).toContain('Accepted the requested evidence');
    expect(text).toContain('Automatically resumed the existing claim');
    expect(text).toContain('Verified the intake requirements');
  });

  it('maps human-review request, Gmail, approval, and resume actions correctly', () => {
    fixture.componentInstance.events = [
      event('claim_review_completed', 'human_review_required', '2026-08-07T12:00:00Z', { conflicts: [{ field: 'policy_number' }] }),
      event('human_review_requested', 'human_review_required', '2026-08-07T12:00:01Z'),
      event('human_review_email_sent', 'human_review_required', '2026-08-07T12:00:02Z'),
      event('human_review_approved', 'human_review_required', '2026-08-07T12:00:03Z'),
      event('human_review_resumed', 'inspection_pending', '2026-08-07T12:00:04Z'),
    ];
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Detected conflicting policy information');
    expect(text).toContain('Paused the claim for human review');
    expect(text).toContain('Sent a secure review request through Gmail');
    expect(text).toContain('Human approval received');
    expect(text).toContain('Automatically resumed the claim after human review');
  });

  it('uses provider-specific language only for confirmed Calendar and Gmail events', () => {
    expect(activityFor(event('inspection_scheduled'))).toBeNull();
    expect(activityFor(event('adjuster_notification_sent'))).toBeNull();
    expect(activityFor(event('google_calendar_event_created'))).toEqual({
      agent: 'DISPATCHER', description: 'Scheduled the inspection in Google Calendar',
    });
    expect(activityFor(event('adjuster_email_sent'))).toEqual({
      agent: 'ADJUSTER DISPATCH', description: 'Sent the final claim handoff through Gmail',
    });
  });

  it('orders events chronologically and conservatively removes duplicate stable events', () => {
    const laterDuplicate = event('human_review_approved', 'human_review_required', '2026-08-07T12:02:00Z', { review_id: 'REV-1' }, 'approval');
    const earlier = event('claim_intake_completed', 'review_processing', '2026-08-07T12:00:00Z');
    const original = event('human_review_approved', 'human_review_required', '2026-08-07T12:01:00Z', { review_id: 'REV-1' }, 'approval');

    const normalized = normalizedEvents([laterDuplicate, original, earlier]);

    expect(normalized.map((item) => item.action)).toEqual(['claim_intake_completed', 'human_review_approved']);
    expect(normalized[1].timestamp).toBe('2026-08-07T12:01:00Z');
  });

  it('never renders raw internal statuses as claimant milestone text', () => {
    fixture.componentInstance.events = [
      event('claim_review_completed', 'human_review_required'),
      event('claim_moved_to_inspection_pending', 'inspection_pending', '2026-08-07T12:00:01Z'),
    ];
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text).not.toContain('human_review_required');
    expect(text).not.toContain('inspection_pending');
  });

  it('returns no activity for an unknown internal action', () => {
    expect(activityFor(event('unknown_internal_action'))).toBeNull();
  });
});
