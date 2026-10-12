import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Merge conditional class names and de-duplicate conflicting Tailwind classes.
 *
 * Reconstructed file: the upstream repository never committed this module
 * because its Python-oriented .gitignore rule `lib/` also matched
 * `web/src/lib/`, which broke `vite build` for every UI component that
 * imports `cn`.
 */
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}
