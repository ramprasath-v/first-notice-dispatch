import { apiBaseUrlFromRuntime, environment } from './environment.production';

describe('production environment', () => {
  it('reads and normalizes the deployed claimant API URL at runtime', () => {
    expect(
      apiBaseUrlFromRuntime({
        apiBaseUrl:
          'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api/',
      }),
    ).toBe(
      'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api',
    );
  });

  it('does not embed localhost or a deployment credential by default', () => {
    expect(environment.apiBaseUrl).toBe('');
  });

  it('forms every claimant route from the production api mount', () => {
    const base = apiBaseUrlFromRuntime({
      apiBaseUrl:
        'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api',
    });

    expect(`${base}/claims`).toBe(
      'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api/claims',
    );
    expect(`${base}/claims/CLM-1`).toBe(
      'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api/claims/CLM-1',
    );
    expect(`${base}/claims/CLM-1/events`).toBe(
      'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api/claims/CLM-1/events',
    );
    expect(`${base}/claims/CLM-1/documents`).toBe(
      'https://firstnotice-claimant-api-4htnargxwa-uc.a.run.app/api/claims/CLM-1/documents',
    );
  });
});
