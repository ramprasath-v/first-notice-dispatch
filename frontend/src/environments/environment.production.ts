interface FirstNoticeRuntimeConfig {
  apiBaseUrl?: string;
}

declare global {
  interface Window {
    __FIRSTNOTICE_CONFIG__?: FirstNoticeRuntimeConfig;
  }
}

export function apiBaseUrlFromRuntime(
  config: FirstNoticeRuntimeConfig | undefined = window.__FIRSTNOTICE_CONFIG__,
): string {
  return config?.apiBaseUrl?.trim().replace(/\/$/, '') ?? '';
}

export const environment = {
  apiBaseUrl: apiBaseUrlFromRuntime(),
};
