// @ts-check
import { defineConfig } from 'astro/config';

import sitemap from '@astrojs/sitemap';
import indexnow from 'astro-indexnow';
import { visit } from 'unist-util-visit';
import trailingSlashRedirect from './src/integrations/trailing-slash-redirect.mjs';

// SITREP convention: per-source sections use the source URL as an h2.
// Auto-link any heading whose only child is a bare http(s) URL so authors
// can write `## https://...` and get a working link without the
// `## [url](url)` boilerplate.
function rehypeAutolinkUrlHeadings() {
  return (tree) => {
    visit(tree, 'element', (node) => {
      if (!/^h[1-6]$/.test(node.tagName)) return;
      if (node.children.length !== 1) return;
      const only = node.children[0];
      if (only.type !== 'text') return;
      const text = only.value.trim();
      if (!/^https?:\/\/\S+$/.test(text)) return;
      node.children = [{
        type: 'element',
        tagName: 'a',
        properties: { href: text, rel: ['nofollow', 'noopener'] },
        children: [{ type: 'text', value: text }],
      }];
    });
  };
}

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
  markdown: {
    rehypePlugins: [rehypeAutolinkUrlHeadings],
  },
  integrations: [
    sitemap(),
    ...(INDEXNOW_KEY ? [indexnow({ key: INDEXNOW_KEY })] : []),
    trailingSlashRedirect(),
  ]
});
