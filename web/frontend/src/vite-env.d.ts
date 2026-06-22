/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional API token surfaced to the client at build time (dev convenience only;
   *  prefer entering it at runtime so it is never baked into the bundle). */
  readonly VITE_DASHBOARD_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
