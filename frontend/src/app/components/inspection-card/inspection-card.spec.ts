import { TestBed } from '@angular/core/testing';
import { InspectionCard } from './inspection-card';

describe('InspectionCard', () => {
  it('renders the scheduled inspection details', async () => {
    await TestBed.configureTestingModule({ imports: [InspectionCard] }).compileComponents();
    const fixture = TestBed.createComponent(InspectionCard);
    fixture.componentInstance.inspection = {
      appointment_id: 'APT-1', inspection_type: 'virtual', status: 'scheduled',
      scheduled_start: '2026-08-08T17:00:00Z', scheduled_end: '2026-08-08T18:00:00Z',
      location_type: 'virtual', location_details: 'Secure video call',
    };
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Virtual');
    expect(fixture.nativeElement.textContent).toContain('Secure video call');
    expect(fixture.nativeElement.textContent).toContain('Scheduled');
  });
});
