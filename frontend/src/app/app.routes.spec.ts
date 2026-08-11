import { routes } from './app.routes';

describe('app routes', () => {
  it('keeps landing, new-claim, and existing status routes distinct', () => {
    expect(routes.some((route) => route.path === '' && route.loadComponent)).toBe(true);
    expect(routes.some((route) => route.path === 'claims/new' && route.loadComponent)).toBe(true);
    expect(routes.some((route) => route.path === 'claims/:claimId' && route.loadComponent)).toBe(true);
  });
});
