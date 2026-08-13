import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { HumanReview } from '../../models/human-review';
import { AdjusterReviewPage, groupEvidenceBySource } from './adjuster-review';

const review: HumanReview = {
  review_id: 'HRV-1',
  claim_id: 'CLM-A1B2C3D4',
  status: 'pending',
  reason: 'A material policy-number conflict requires human verification.',
  briefing: {
    reason: 'A material policy-number conflict requires human verification.',
    summary: 'Claim submission and police report contain different policy numbers.',
    known_facts: ['Claim type: auto_collision'],
    conflicts: ['policy_number: POL-1001 versus POL-9999'],
    unresolved_questions: ['Verify the correct value for policy_number.'],
    recommended_next_action: 'Verify the correct policy identifier.',
    confidence: 0.9,
  },
  recommended_remediation: {
    type: 'enter_text',
    label: 'Ask the claimant to confirm the policy number.',
    instruction: 'Please confirm your policy number.',
    field_name: 'policy_number',
    can_request: true,
  },
  ai_recommendation: 'Physical inspection recommended. Rear damage is supported.',
  claim_snapshot: {
    incident: 'Rear impact.', drivable: true,
    police_report_status: 'Validated', damage_evidence_status: 'Validated',
  },
  evidence_comparison: [
    { source: 'police-report.pdf', finding: 'Rear-end impact is documented.' },
    { source: 'rear.jpg', finding: 'Rear bumper damage is visible.' },
  ],
  resolution_history: ['FirstNotice validated requested evidence.'],
  expires_at: '2026-08-08T02:00:00Z',
};

describe('AdjusterReviewPage', () => {
  const api = {
    getHumanReview: vi.fn(),
    approveHumanReview: vi.fn(),
    requestHumanReviewCorrection: vi.fn(),
    continueManualHandling: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    api.getHumanReview.mockReturnValue(of(review));
    api.approveHumanReview.mockReturnValue(of({
      review_id: 'HRV-1', claim_id: review.claim_id, status: 'approved',
      event_id: 'approve-event', duplicate: false,
      message: 'Review approved. FirstNotice has resumed processing the claim.',
    }));
    api.requestHumanReviewCorrection.mockReturnValue(of({
      review_id: 'HRV-1', claim_id: review.claim_id, status: 'correction_requested',
      event_id: 'correction-event', duplicate: false,
      message: 'Correction requested. The claimant workflow will update automatically.',
    }));
    api.continueManualHandling.mockReturnValue(of({
      review_id: 'HRV-1', claim_id: review.claim_id, status: 'manual_handling',
      event_id: 'manual-event', duplicate: false,
      message: 'Manual handling recorded. No claimant action was requested.',
    }));
  });

  async function create(): Promise<ComponentFixture<AdjusterReviewPage>> {
    await TestBed.configureTestingModule({
      imports: [AdjusterReviewPage],
      providers: [
        { provide: ClaimApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => 'secure-token' } } } },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(AdjusterReviewPage);
    fixture.detectChanges();
    return fixture;
  }

  it('loads a concise inspection packet while AI analysis is collapsed', async () => {
    const fixture = await create();
    expect(api.getHumanReview).toHaveBeenCalledWith('secure-token');
    expect(fixture.nativeElement.textContent).toContain('CLM-A1B2C3D4');
    expect(fixture.nativeElement.textContent).toContain('Physical inspection recommended');
    expect(fixture.nativeElement.textContent).toContain('Rear impact');
    expect(fixture.nativeElement.textContent).toContain('Rear bumper damage is visible');
    expect(fixture.nativeElement.textContent).toContain('Autonomous resolution history');
    const analysis = fixture.nativeElement.querySelector('details.analysis-details') as HTMLDetailsElement;
    expect(analysis.open).toBe(false);
    expect(analysis.textContent).toContain('POL-1001 versus POL-9999');
  });

  it('expands the detailed AI analysis on request', async () => {
    const fixture = await create();
    const analysis = fixture.nativeElement.querySelector('details.analysis-details') as HTMLDetailsElement;
    analysis.querySelector('summary')?.dispatchEvent(new MouseEvent('click'));
    fixture.detectChanges();
    expect(analysis.open).toBe(true);
  });

  it('renders only the two product decisions with no document selector', async () => {
    const fixture = await create();
    const labels = [...fixture.nativeElement.querySelectorAll('.actions button')]
      .map((button: HTMLButtonElement) => button.textContent?.trim());
    expect(labels).toEqual(['Request More Info', 'Approve Inspection']);
    expect(fixture.nativeElement.querySelector('select')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Request Text Correction');
    expect(fixture.nativeElement.textContent).not.toContain('Request Replacement Evidence');
  });

  it('approves through the token endpoint and disables both buttons', async () => {
    const fixture = await create();
    fixture.nativeElement.querySelector('.primary').click();
    fixture.detectChanges();
    expect(api.approveHumanReview).toHaveBeenCalledWith('secure-token');
    expect(fixture.nativeElement.textContent).toContain('resumed processing');
    expect(fixture.nativeElement.querySelectorAll('button')).toHaveLength(0);
  });

  it('requests more information with one free-text instruction', async () => {
    const fixture = await create();
    fixture.componentInstance.requestMoreInformation();
    fixture.componentInstance.decisionNote = 'Please upload a clearer rear damage photo.';
    fixture.componentInstance.requestMoreInformation();
    fixture.detectChanges();
    expect(api.requestHumanReviewCorrection).toHaveBeenCalledWith(
      'secure-token', 'Please upload a clearer rear damage photo.',
    );
    expect(fixture.nativeElement.textContent).toContain('Correction requested');
  });

  it('keeps a human-review checkpoint manual without claimant remediation', async () => {
    api.getHumanReview.mockReturnValue(of({
      ...review,
      checkpoint_status: 'human_review_required',
    }));
    const fixture = await create();

    fixture.componentInstance.continueManualReview();
    fixture.detectChanges();

    expect(api.continueManualHandling).toHaveBeenCalledWith('secure-token');
    expect(api.approveHumanReview).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Manual handling recorded');
  });

  it('loads a repeated review cycle with its own current recommendation', async () => {
    api.getHumanReview.mockReturnValue(of({
      ...review,
      review_id: 'HRV-2',
      generation: 2,
      recommended_remediation: {
        type: 'upload_document',
        label: 'Request a replacement damage photo.',
        instruction: 'Please upload the correct damage photo for this claim.',
        document_type: 'damage_evidence',
        can_request: true,
      },
      source_references: [
        {
          filename: 'initial-damage.jpg',
        },
      ],
    }));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Inspection Decision');
    expect(fixture.nativeElement.textContent).toContain('initial-damage.jpg');
    expect(fixture.nativeElement.querySelector('select')).toBeNull();
  });

  it('does not expose document IDs, evidence types, or replacement controls', async () => {
    api.getHumanReview.mockReturnValue(of({
      ...review,
      source_references: [
        { filename: 'current-policy.pdf' },
      ],
    }));
    const fixture = await create();
    fixture.componentInstance.requestMoreInformation();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('textarea')).toHaveLength(1);
    expect(fixture.nativeElement.querySelector('select')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('DOC-POLICY');
    expect(fixture.nativeElement.textContent).not.toContain('Evidence type');
    expect(fixture.nativeElement.textContent).not.toContain('Replace existing');
  });

  it('does not submit an empty more-info instruction', async () => {
    api.getHumanReview.mockReturnValue(of({
      ...review,
      recommended_remediation: {
        type: 'upload_document', label: 'Manual evidence selection required.',
        instruction: 'Multiple evidence artifacts could require replacement.',
        document_type: 'damage_evidence', can_request: false,
      },
    }));
    const fixture = await create();
    const request = fixture.nativeElement.querySelector('.actions .secondary') as HTMLButtonElement;
    request.click();
    fixture.detectChanges();
    const send = fixture.nativeElement.querySelector('.actions .secondary') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    send.click();
    expect(api.requestHumanReviewCorrection).not.toHaveBeenCalled();
  });

  it('shows a safe validation message while leaving a vague request pending', async () => {
    api.requestHumanReviewCorrection.mockReturnValue(throwError(() =>
      new HttpErrorResponse({
        status: 409,
        error: { detail: 'Request a specific supported document or correction.' },
      }),
    ));
    const fixture = await create();
    fixture.componentInstance.requestMoreInformation();
    fixture.componentInstance.decisionNote = 'I need something else.';
    fixture.componentInstance.requestMoreInformation();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'Request a specific supported document or correction.',
    );
    expect(fixture.componentInstance.review()?.status).toBe('pending');
  });

  it('renders an expired link safely', async () => {
    api.getHumanReview.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 410 })));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('review link has expired');
  });

  it('renders an invalid link safely', async () => {
    api.getHumanReview.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('invalid or unavailable');
  });

  it('does not render claimant upload controls', async () => {
    const fixture = await create();
    expect(fixture.nativeElement.querySelector('app-missing-documents')).toBeNull();
    expect(fixture.nativeElement.querySelector('input[type=file]')).toBeNull();
  });

  it('groups and deduplicates current evidence findings by source document', async () => {
    api.getHumanReview.mockReturnValue(of({
      ...review,
      evidence_comparison: [
        { source: 'vehicle_damage_license.jpg', finding: 'Plate 7ABX123 is readable.' },
        { source: 'vehicle_damage_license.jpg', finding: 'Rear bumper damage is visible.' },
        { source: 'vehicle_damage_license.jpg', finding: 'Rear bumper damage is visible.' },
        { source: 'police-report.pdf', finding: 'Rear-end collision is documented.' },
      ],
    }));
    const fixture = await create();
    const cards = fixture.nativeElement.querySelectorAll('.evidence-card');
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain('Current damage photo');
    expect(cards[0].querySelectorAll('li')).toHaveLength(2);
    expect(fixture.nativeElement.textContent.match(/Rear bumper damage is visible\./g)).toHaveLength(1);
  });

  it('builds one group per source using case-insensitive finding deduplication', () => {
    expect(groupEvidenceBySource([
      { source: 'rear.jpg', finding: 'Plate readable' },
      { source: 'rear.jpg', finding: 'plate   readable' },
    ])).toEqual([{
      source: 'rear.jpg', label: 'Current damage photo', findings: ['Plate readable'],
    }]);
  });

  it('renders recommendation, snapshot, collapsed analysis, and both decisions', async () => {
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('AI recommendation');
    expect(fixture.nativeElement.textContent).toContain('Claim snapshot');
    expect((fixture.nativeElement.querySelector('.analysis-details') as HTMLDetailsElement).open).toBe(false);
    expect(fixture.nativeElement.querySelectorAll('.actions button')).toHaveLength(2);
  });
});
