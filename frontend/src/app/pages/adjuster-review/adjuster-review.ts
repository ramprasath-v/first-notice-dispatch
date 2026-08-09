import { HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { finalize } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { HumanReview } from '../../models/human-review';

@Component({
  selector: 'app-adjuster-review-page',
  imports: [FormsModule],
  templateUrl: './adjuster-review.html',
  styleUrl: './adjuster-review.scss',
})
export class AdjusterReviewPage {
  private readonly api = inject(ClaimApiService);
  private readonly routeToken = inject(ActivatedRoute).snapshot.paramMap.get('token') ?? '';
  private readonly token = this.routeToken === 'checkpoint'
    ? window.sessionStorage.getItem('firstnotice.reviewToken') ?? ''
    : this.routeToken;
  readonly review = signal<HumanReview | null>(null);
  readonly loading = signal(true);
  readonly deciding = signal(false);
  readonly error = signal('');
  readonly completedMessage = signal('');
  decisionNote = '';

  constructor() {
    this.api.getHumanReview(this.token).subscribe({
      next: (review) => {
        this.review.set(review);
        this.loading.set(false);
        if (review.status !== 'pending') {
          this.completedMessage.set('This review has already been completed.');
        }
      },
      error: (error: HttpErrorResponse) => {
        this.loading.set(false);
        this.error.set(
          error.status === 410
            ? 'This review link has expired.'
            : 'This review link is invalid or unavailable.',
        );
      },
    });
  }

  approve(): void {
    this.decide('approve');
  }

  requestMoreInformation(): void {
    if (!this.review()?.recommended_remediation.can_request) return;
    this.decide('correction');
  }

  comparisonFacts(): Array<{ source: string; finding: string }> {
    return (this.review()?.briefing.known_facts ?? []).flatMap((fact) => {
      const separator = fact.indexOf(':');
      if (separator < 1) return [];
      const rawSource = fact.slice(0, separator).trim();
      const finding = fact.slice(separator + 1).trim();
      const normalized = rawSource.toLowerCase();
      const source = normalized.includes('police')
        ? 'Police report'
        : normalized.includes('followup') || normalized.includes('follow-up')
          ? 'Follow-up evidence'
          : normalized.match(/\.(jpg|jpeg|png|webp|heic)$/)
            ? 'Submitted photo'
            : rawSource.replaceAll('_', ' ');
      return finding ? [{ source, finding }] : [];
    }).slice(0, 3);
  }

  humanize(value: string): string {
    return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  private decide(action: 'approve' | 'correction'): void {
    if (this.deciding() || this.review()?.status !== 'pending') return;
    this.deciding.set(true);
    this.error.set('');
    const request = action === 'approve'
      ? this.api.approveHumanReview(this.token, this.decisionNote)
      : this.api.requestHumanReviewCorrection(this.token, this.decisionNote);
    request.pipe(finalize(() => this.deciding.set(false))).subscribe({
      next: (result) => {
        window.sessionStorage.removeItem('firstnotice.reviewToken');
        this.review.update((review) => review ? { ...review, status: result.status } : review);
        this.completedMessage.set(result.message);
      },
      error: (error: HttpErrorResponse) => {
        this.error.set(
          error.status === 410
            ? 'This review link has expired.'
            : error.status === 409
              ? 'This review has already been completed.'
              : 'We could not record the decision. Please try again.',
        );
      },
    });
  }
}
