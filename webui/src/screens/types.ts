/** Props every routed screen receives from the shell. */
export interface ScreenProps {
  /** Deep-link id from the hash (`#/projects/p-1` → 'p-1'). */
  id: string;
  /** Parsed query string from the hash. */
  query: URLSearchParams;
}

export interface ScreenMeta {
  /** i18n key of the navigation label. */
  labelKey: string;
  /** Short one-line description shown under the page title. */
  descriptionKey: string;
}
