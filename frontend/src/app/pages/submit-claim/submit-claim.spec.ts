import { HttpResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { SubmitClaim } from './submit-claim';

describe('SubmitClaim', () => {
  let fixture: ComponentFixture<SubmitClaim>;
  let component: SubmitClaim;
  const api = { submitClaim: vi.fn() };
  const router = { navigate: vi.fn() };

  beforeEach(async () => {
    vi.clearAllMocks();
    await TestBed.configureTestingModule({
      imports: [SubmitClaim],
      providers: [
        { provide: ClaimApiService, useValue: api },
        { provide: Router, useValue: router },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(SubmitClaim);
    component = fixture.componentInstance;
  });

  it('rejects submission without a damage photo', () => {
    component.form.controls.incidentDescription.setValue('Rear-ended');
    component.submit();
    fixture.detectChanges();
    expect(api.submitClaim).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('Add at least one damage photo');
  });

  it('accepts a damage photo without a police report', () => {
    api.submitClaim.mockReturnValue(of(new HttpResponse({ body: {
      claim_id: 'CLM-ABC12345', status: 'new', event_id: 'evt', message: 'received',
    }})));
    component.form.controls.incidentDescription.setValue('Rear-ended at a stoplight');
    component.damagePhotos.set([new File(['x'], 'damage.jpg', { type: 'image/jpeg' })]);

    component.submit();

    expect(api.submitClaim).toHaveBeenCalledOnce();
    expect(api.submitClaim.mock.calls[0][0].policeReport).toBeUndefined();
    expect(router.navigate).toHaveBeenCalledWith(['/claims', 'CLM-ABC12345']);
  });

  it('reuses the same idempotency key when a submission is retried', () => {
    api.submitClaim.mockReturnValueOnce(throwError(() => new Error('network')))
      .mockReturnValueOnce(of(new HttpResponse({ body: {
        claim_id: 'CLM-ABC12345', status: 'new', event_id: 'evt', message: 'received',
      }})));
    component.form.controls.incidentDescription.setValue('Rear-ended');
    component.damagePhotos.set([new File(['x'], 'damage.jpg', { type: 'image/jpeg' })]);

    component.submit();
    component.submit();

    expect(api.submitClaim.mock.calls[0][1]).toBe(api.submitClaim.mock.calls[1][1]);
    expect(component.requestError()).toBe('');
  });
});
