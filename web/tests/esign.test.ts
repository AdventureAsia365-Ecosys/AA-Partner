// AA-955 (CRM-25): the e-sign v1 helpers of the web: the signing body (every envelope document as shown, the
// declination only when declined, the minor signed for), the BFF lines of the agency routes, the refusal wording and
// the client strings (EN and VI).
import { describe, expect, it } from "vitest";
import { BFF_ROUTES, matchBff } from "@/lib/bff";
import { clientMessages } from "@/lib/clientMessages";
import { MESSAGES } from "@/lib/i18n";
import { AGREEMENT_KINDS, clientErrorKey, proposalErrorKey, signBody, type PortalAgreements, type PortalEnvelope } from "@/lib/proposals";

const UUID = "00000000-0000-4000-8000-000000000001";
const doc = (kind: "booking_terms" | "waiver" | "insurance_declination", id: string) => ({
  kind,
  doc_id: id,
  version_label: "v1",
  body_text: "text",
  body_sha256: id.repeat(2),
});
const box: PortalAgreements = {
  documents: [doc("booking_terms", "t"), doc("waiver", "w")],
  insurance_declination: doc("insurance_declination", "d"),
  envelopes: [],
  guardian_signs_for_me: false,
};
const self: PortalEnvelope = { traveler_id: "me", for: "self", minor_number: null, signed: false, signed_at: null, signer_role: null, insurance_choice: null };
const child: PortalEnvelope = { ...self, traveler_id: "kid", for: "minor", minor_number: 1 };

describe("signBody", () => {
  it("sends every document as shown and the declination only when declined", () => {
    const declined = signBody(box, self, { code: " 123456 ", fullName: "Grace Lee", agree: true, insurance: "declined" });
    expect(declined).toEqual({
      code: "123456",
      full_name: "Grace Lee",
      agree: true,
      documents: [
        { doc_id: "t", doc_sha256: "tt" },
        { doc_id: "w", doc_sha256: "ww" },
        { doc_id: "d", doc_sha256: "dd" },
      ],
      insurance: "declined",
    });
    const insured = signBody(box, child, { code: "1", fullName: "G", agree: true, insurance: "insured" });
    expect((insured.documents as unknown[]).length).toBe(2);
    expect(insured.traveler_id).toBe("kid");
    const noQuestion = signBody({ ...box, insurance_declination: null }, self, { code: "1", fullName: "G", agree: true, insurance: null });
    expect("insurance" in noQuestion).toBe(false);
  });
});

describe("e-sign routes and wording", () => {
  it("allows the two agency routes through the BFF", () => {
    expect(matchBff("GET", `trips/${UUID}/agreements`)).toBeTruthy();
    expect(matchBff("POST", `signatures/${UUID}/envelope`)).toBeTruthy();
    expect(matchBff("GET", `signatures/${UUID}/envelope`)).toBeFalsy();
    expect(BFF_ROUTES.filter((r) => r.card === "AA-955").length).toBe(2);
  });

  it("words the refusals and ships the client strings in both languages", () => {
    for (const code of ["guardian_signs", "not_guardian", "esign_not_configured", "seal_unavailable", "nothing_to_accept"]) {
      expect(clientErrorKey(code)).toBe(`client.error.${code}`);
    }
    expect(proposalErrorKey("terms_reaccept_required")).toBe("proposal.error.terms_reaccept_required");
    for (const lang of ["en", "vi"] as const) {
      const dict = clientMessages(lang);
      expect(dict["client.envelope.minor"]).toContain("{n}");
      expect(dict["client.termsUpdatedTitle"]).toContain("{version}");
      for (const kind of AGREEMENT_KINDS) {
        expect(MESSAGES[lang][`agreements.kind.${kind}`]).toBeTruthy();
        expect(dict[`client.doc.${kind}`]).toContain("{version}");
      }
    }
  });
});
