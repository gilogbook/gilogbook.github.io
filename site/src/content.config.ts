import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

// 글은 src/content/posts/ 에 둔다.
// src/pages/ 밖에 있으므로 파일이 있어도 자동으로 페이지가 만들어지지 않는다.
// 실제 페이지 생성은 src/pages/posts/[...slug].astro 가 pubDate를 보고 결정한다.
const posts = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/posts' }),
  schema: z.object({
    title: z.string(),
    description: z.string().optional(),
    pubDate: z.coerce.date(),
    category: z.string().optional(),
  }),
});

export const collections = { posts };
