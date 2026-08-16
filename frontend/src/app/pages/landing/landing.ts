import { Component, inject } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';

@Component({
  selector: 'app-landing-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './landing.html',
  styleUrl: './landing.scss',
})
export class LandingPage {
  private readonly router = inject(Router);
  private readonly fb = inject(FormBuilder);

  readonly form = this.fb.nonNullable.group({
    claimId: ['', Validators.required],
  });

  continueClaim(): void {
    const claimId = this.form.controls.claimId.value.trim().toUpperCase();
    if (!claimId) return;
    void this.router.navigate(['/claims', claimId]);
  }
}
