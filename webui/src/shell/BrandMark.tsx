/**
 * BrandMark.tsx — the product glyph, drawn with currentColor so it follows the
 * theme.  Small enough to sit in the side-nav heading without an asset request.
 */

import type { ReactNode, SVGProps } from 'react';

export function BrandMark(props: SVGProps<SVGSVGElement>): ReactNode {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false" {...props}>
      <path d="M12 2.8 21 7.4l-9 4.6L3 7.4l9-4.6Z" fill="currentColor" opacity=".95" />
      <path d="m3 12 9 4.6 9-4.6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" opacity=".75" />
      <path d="m3 16.6 9 4.6 9-4.6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" opacity=".45" />
    </svg>
  );
}
