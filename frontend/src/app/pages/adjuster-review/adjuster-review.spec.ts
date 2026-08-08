import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { HumanReview } from '../../models/human-review';
import { AdjusterReviewPage } from './adjuster-review';

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
  expires_at: '2026-08-08T02:00:00Z',
};

describe('AdjusterReviewPage', () => {
  const api = {
    getHumanReview: vi.fn(),
    approveHumanReview: vi.fn(),
    requestHumanReviewCorrection: vi.fn(),
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

  it('loads a valid token and renders briefing and conflicts', async () => {
    const fixture = await create();
    expect(api.getHumanReview).toHaveBeenCalledWith('secure-token');
    expect(fixture.nativeElement.textContent).toContain('CLM-A1B2C3D4');
    expect(fixture.nativeElement.textContent).toContain('POL-1001 versus POL-9999');
  });

  it('approves through the token endpoint and disables both buttons', async () => {
    const fixture = await create();
    fixture.nativeElement.querySelector('.primary').click();
    fixture.detectChanges();
    expect(api.approveHumanReview).toHaveBeenCalledWith('secure-token', '');
    expect(fixture.nativeElement.textContent).toContain('resumed processing');
    expect(fixture.nativeElement.querySelectorAll('button')).toHaveLength(0);
  });

  it('requests correction through the distinct endpoint', async () => {
    const fixture = await create();
    fixture.nativeElement.querySelector('.secondary').click();
    fixture.detectChanges();
    expect(api.requestHumanReviewCorrection).toHaveBeenCalledWith('secure-token', '');
    expect(fixture.nativeElement.textContent).toContain('Correction requested');
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
});
