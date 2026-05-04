import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const compliancePosture = z.enum([
  'blue-uas-framework',
  'blue-uas-cleared',
  'ndaa-compliant',
  'not-eligible',
  'unknown',
]);

const readinessPosture = z.enum([
  'production',
  'low-rate-production',
  'prototype',
  'engineering-services',
  'unknown',
]);

const fact = z.object({
  label: z.string(),
  // value omitted/null → renders as italic "Not disclosed (checked <reviewed_at>)"
  value: z.string().optional(),
  // ref omitted → no Source Drawer trigger
  ref: z.string().optional(),
  snippet: z.string().optional(),
  source_url: z.string().url().optional(),
  fetched_at: z.string().optional(), // DTG e.g. "251147Z APR 26"
  renderer: z.string().optional(),   // "httpx (200 OK) · cleaned 14,580 chars"
});

const product = z.object({
  name: z.string(),
  category: z.string(),
  descriptor: z.string().optional(),
  granularity: z.string().optional(),
  readiness: z.string().optional(),
  ndaa: z.string().optional(),
  blue_uas: z.string().optional(),
  notes: z.string().optional(),
  source_url: z.string().url().optional(),
  snippet: z.string().optional(),
});

const vendor = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/vendor' }),
  schema: z.object({
    designation: z.string(),                              // "A.0042"
    canonical_name: z.string(),
    legal_name: z.string().optional(),
    homepage_url: z.string().url().optional(),
    hq_city: z.string().optional(),
    hq_region: z.string().optional(),                     // US state or country region
    country_iso2: z.string().length(2),
    country_name: z.string(),
    primary_category: z.string(),
    categories: z.array(z.string()).default([]),
    cross_listings: z.array(z.string()).default([]),      // other designations
    compliance_posture: compliancePosture,
    readiness_posture: readinessPosture,
    certifications: z.array(z.string()).default([]),
    domains: z.array(z.enum(['uav', 'ugv', 'usv', 'uuv'])).default([]),
    founded_year: z.number().int().optional(),
    ownership_type: z.string().optional(),
    employee_count: z.number().int().optional(),
    product_lines: z.array(z.string()).default([]),
    products: z.array(product).default([]),
    reviewed_at: z.string(),                              // ISO date YYYY-MM-DD
    tagline: z.string(),                                  // one-sentence editorial
    facts: z.array(fact).default([]),                     // detail-page fact rows
  }),
});

const sector = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/sector' }),
  schema: z.object({
    letter: z.string().length(1).optional(),
    display_name: z.string(),                             // "Propulsion Electronics"
    is_meta: z.boolean().default(false),
    intro_paragraph: z.string(),
  }),
});

const sitrep = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/sitrep' }),
  schema: z.object({
    issue_id: z.string(),                                 // "2026-w17"
    dtg: z.string(),                                      // "251200Z APR 26"
    subject: z.string(),
    origin: z.string().default('UxV Market Editorial'),
  }),
});

export const collections = { vendor, sector, sitrep };
