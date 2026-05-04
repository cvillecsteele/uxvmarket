// @ts-check
import { defineConfig } from 'astro/config';

import sitemap from '@astrojs/sitemap';
import indexnow from 'astro-indexnow';
import trailingSlashRedirect from './src/integrations/trailing-slash-redirect.mjs';

// Set INDEXNOW_KEY in the build env (GH Actions secret of the same name)
// to enable IndexNow pings on build. When missing, the integration is
// skipped — the site still builds fine; search engines just don't get
// notified of new content. Add a matching `<key>.txt` file to public/
// so engines can verify ownership.
const INDEXNOW_KEY = process.env.INDEXNOW_KEY;

export default defineConfig({
  site: 'https://uxvmarket.com',
  trailingSlash: 'never',
  build: {
    format: 'file'
  },
  integrations: [
    sitemap(),
    ...(INDEXNOW_KEY ? [indexnow({ key: INDEXNOW_KEY })] : []),
    trailingSlashRedirect(),
  ]
});
