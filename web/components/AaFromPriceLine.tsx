// web/components/AaFromPriceLine.tsx
// AA-1188: the one line an agent reads about AA's public price of a tour where it adds an AA product (the proposal
// builder's AA drawer and the ledger's AA item form): "AA's public price starts from {price} per person." from the
// product's price.from.v1 "from" price, else "Price on request.", the same answer as the studio cards and drawer.
import { K } from "@/app/_kit";
import type { AaProduct } from "@/lib/aa";
import { fmt, t, type Lang } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";

/** The text of the line: the "from" price per person, else "Price on request." */
export function aaFromPriceText(fromPrice: AaProduct["from_price"], lang: Lang, locale: string): string {
  if (!fromPrice) return t(lang, "aa.priceOnRequest");
  return fmt(t(lang, "aa.fromPrice"), { price: formatMoney(fromPrice.amount, fromPrice.currency, locale) });
}

export function AaFromPriceLine({
  fromPrice,
  lang,
  locale,
}: {
  fromPrice: AaProduct["from_price"];
  lang: Lang;
  locale: string;
}) {
  return (
    <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="aa-from-price">
      {aaFromPriceText(fromPrice, lang, locale)}
    </p>
  );
}
