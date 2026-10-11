// Цветные логотипы площадок. В lucide брендовых значков больше нет, поэтому
// пути и фирменные цвета берутся из simple-icons (CC0). Логотип — это
// категория аккаунта: по нему глаз отличает YouTube от TikTok без подписи.

import { Radio } from "lucide-react";
import { siInstagram, siTiktok, siYoutube } from "simple-icons";

interface Brand {
  title: string;
  path: string;
  hex: string;
}

const BRANDS: Record<string, Brand> = {
  youtube: siYoutube,
  instagram: siInstagram,
  tiktok: siTiktok,
};

export function BrandIcon({ network, className }: { network: string; className?: string }) {
  const brand = BRANDS[network];
  if (!brand) return <Radio className={className} />;
  // фирменный чёрный (TikTok) на тёмной теме не виден — берём цвет текста
  const fill = brand.hex.toUpperCase() === "000000" ? "currentColor" : `#${brand.hex}`;
  return (
    <svg viewBox="0 0 24 24" className={className} role="img" aria-label={brand.title}>
      <path d={brand.path} fill={fill} />
    </svg>
  );
}
