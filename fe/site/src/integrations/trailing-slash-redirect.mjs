import { readdir, mkdir, writeFile } from 'node:fs/promises';
import { join, basename } from 'node:path';

/**
 * Astro integration that creates redirect stubs for trailing-slash URLs.
 *
 * For every `page.html` in the build output, generates `page/index.html`
 * with a <meta http-equiv="refresh"> redirect to `/page`. This ensures
 * that `/page/` doesn't 404 on GitHub Pages.
 *
 * Skips `index.html` (the site root) and any file already inside a directory
 * (e.g. `blog/post.html` → `blog/post/index.html` redirecting to `/blog/post`).
 */
export default function trailingSlashRedirect() {
  return {
    name: 'trailing-slash-redirect',
    hooks: {
      'astro:build:done': async ({ dir }) => {
        const outDir = dir.pathname;
        const created = await processDir(outDir, outDir);
        console.log(`[trailing-slash-redirect] created ${created} redirect stub(s)`);
      },
    },
  };
}

async function processDir(dir, outDir) {
  let count = 0;
  const entries = await readdir(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = join(dir, entry.name);

    if (entry.isDirectory()) {
      count += await processDir(fullPath, outDir);
    } else if (entry.name.endsWith('.html') && entry.name !== 'index.html') {
      const slug = basename(entry.name, '.html');
      const stubDir = join(dir, slug);
      const relative = stubDir.slice(outDir.length).replace(/\/$/, '');
      const target = '/' + relative.replace(/\\/g, '/');

      await mkdir(stubDir, { recursive: true });
      await writeFile(
        join(stubDir, 'index.html'),
        `<!doctype html><meta http-equiv="refresh" content="0;url=${target}"><link rel="canonical" href="${target}"><title>Redirecting</title>`
      );
      count++;
    }
  }

  return count;
}
