import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () =>
      import('./pages/landing/landing').then((m) => m.LandingPage),
  },
  {
    path: 'claims/new',
    loadComponent: () =>
      import('./pages/submit-claim/submit-claim').then((m) => m.SubmitClaim),
  },
  {
    path: 'adjuster/review/:token',
    loadComponent: () =>
      import('./pages/adjuster-review/adjuster-review').then(
        (m) => m.AdjusterReviewPage,
      ),
  },
  {
    path: 'claims/:claimId',
    loadComponent: () =>
      import('./pages/claim-status/claim-status').then((m) => m.ClaimStatusPage),
  },
  { path: '**', redirectTo: '' },
];
