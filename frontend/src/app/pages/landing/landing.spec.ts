import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { LandingPage } from './landing';

describe('LandingPage', () => {
  let fixture: ComponentFixture<LandingPage>;
  let router: Router;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [LandingPage],
      providers: [provideRouter([])],
    }).compileComponents();
    router = TestBed.inject(Router);
    vi.spyOn(router, 'navigate').mockResolvedValue(true);
    fixture = TestBed.createComponent(LandingPage);
    fixture.detectChanges();
  });

  it('offers new and existing claim actions', () => {
    expect(fixture.nativeElement.querySelector('a[href="/claims/new"]')).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Continue existing claim');
  });

  it('navigates to a normalized existing claim ID', () => {
    fixture.componentInstance.form.controls.claimId.setValue(' clm-a1b2c3d4 ');
    fixture.componentInstance.continueClaim();
    expect(router.navigate).toHaveBeenCalledWith(['/claims', 'CLM-A1B2C3D4']);
  });
});
