import { HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { finalize } from 'rxjs';
import { ClaimApiService } from '../../core/services/claim-api.service';
import { HumanReview } from '../../models/human-review';

export interface EvidenceGroup {
  source: string;
  label: string;
  findings: string[];
}

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
  readonly requestInfoOpen = signal(false);
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
    if (!this.requestInfoOpen()) {
      this.requestInfoOpen.set(true);
      return;
    }
    if (!this.decisionNote.trim()) return;
    this.decide('correction');
  }

  continueManualReview(): void {
    if (this.deciding() || this.review()?.status !== 'pending') return;
    this.requestInfoOpen.set(false);
    this.deciding.set(true);
    this.error.set('');
    this.api.continueManualHandling(this.token)
      .pipe(finalize(() => this.deciding.set(false)))
      .subscribe({
        next: (result) => {
          window.sessionStorage.removeItem('firstnotice.reviewToken');
          this.review.update((review) => review ? { ...review, status: result.status } : review);
          this.completedMessage.set(result.message);
        },
        error: () => this.error.set('We could not record the decision. Please try again.'),
      });
  }

  comparisonFacts(): Array<{ source: string; finding: string }> {
    const persisted = this.review()?.evidence_comparison ?? [];
    if (persisted.length) return persisted;
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
    });
  }

  groupedEvidence(): EvidenceGroup[] {
    return groupEvidenceBySource(this.comparisonFacts());
  }

  snapshotEntries(): Array<{ label: string; value: string }> {
    const labels: Record<string, string> = {
      incident: 'Incident', incident_date: 'Date', vehicle: 'Vehicle', plate: 'Plate',
      license_plate: 'Plate', policy_number: 'Policy', claim_type: 'Claim type',
      drivable: 'Vehicle drivable', police_report_status: 'Police report',
      damage_evidence_status: 'Damage evidence',
    };
    return Object.entries(this.review()?.claim_snapshot ?? {}).flatMap(([key, value]) =>
      value === null || value === ''
        ? []
        : [{ label: labels[key] ?? this.humanize(key), value: typeof value === 'boolean' ? (value ? 'Yes' : 'No') : String(value) }],
    );
  }

  humanize(value: string): string {
    return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  private decide(action: 'approve' | 'correction'): void {
    if (this.deciding() || this.review()?.status !== 'pending') return;
    this.deciding.set(true);
    this.error.set('');
    const request = action === 'approve'
      ? this.api.approveHumanReview(this.token)
      : this.api.requestHumanReviewCorrection(
          this.token,
          this.decisionNote,
        );
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
              ? (typeof error.error?.detail === 'string'
                  ? error.error.detail
                  : 'This review has already been completed.')
              : 'We could not record the decision. Please try again.',
        );
      },
    });
  }
}

export function groupEvidenceBySource(facts: Array<{ source: string; finding: string }>): EvidenceGroup[] {
  const groups = new Map<string, EvidenceGroup & { seen: Set<string> }>();
  for (const fact of facts) {
    const source = fact.source.trim() || 'Current evidence';
    const finding = fact.finding.trim();
    if (!finding) continue;
    const key = source.toLocaleLowerCase();
    const existing = groups.get(key) ?? {
      source,
      label: evidenceLabel(source),
      findings: [],
      seen: new Set<string>(),
    };
    const findingKey = finding.toLocaleLowerCase().replace(/\s+/g, ' ');
    if (!existing.seen.has(findingKey)) {
      existing.seen.add(findingKey);
      existing.findings.push(finding);
    }
    groups.set(key, existing);
  }
  return [...groups.values()].map(({ seen: _seen, ...group }) => group);
}

function evidenceLabel(source: string): string {
  const normalized = source.toLocaleLowerCase();
  if (normalized.includes('police') || normalized.endsWith('.pdf')) return 'Police report';
  if (/\.(jpe?g|png|webp|heic)$/.test(normalized)) return 'Current damage photo';
  return 'Current evidence';
}
