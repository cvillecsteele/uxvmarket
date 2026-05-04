// @ts-check
import { defineConfig } from 'astro/config';

import sitemap from '@astrojs/sitemap';
import indexnow from 'astro-indexnow';
import trailingSlashRedirect from './src/integrations/trailing-slash-redirect.mjs';

// Placeholder IndexNow key. Replace with a real generated UUID when wiring DNS,
// and add a matching <key>.txt file to public/ so search engines can verify.
const INDEXNOW_KEY = process.env.INDEXNOW_KEY ?? 'a3b8c19d8f7e4f6a9b2c8d3e5f1a7b9c';

export default defineConfig({
  site: 'https://uxvmarket.com',
  trailingSlash: 'never',
  build: {
    format: 'file'
  },
  integrations: [
    sitemap(),
    indexnow({ key: INDEXNOW_KEY }),
    trailingSlashRedirect(),
  ]
});
