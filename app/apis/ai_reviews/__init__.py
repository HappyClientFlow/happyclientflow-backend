"""
This API module is responsible for generating AI-powered Google reviews based on client feedback.

It provides an endpoint that receives feedback details, constructs a prompt for the OpenAI API,
and returns a generated review text. This is used in the final step of the positive
feedback flow on the frontend to assist clients in writing their reviews.

Variety model (Phase 1): each generation draws an INDEPENDENT random "reviewer
persona" — length corridor, tone, structure, and which of the guest's own answers
to emphasize. This makes reviews read like different real people rather than one
author. There is deliberately NO per-customer or per-company fixed voice (a fixed
voice would make a single business's reviews sound more alike, the opposite of the goal).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import databutton as db
from openai import OpenAI
import random

from app.libs.reminder_scheduling import feedback_high_satisfaction_min

# Initialize OpenAI client
try:
    client = OpenAI(api_key=db.secrets.get("OPENAI_API_KEY"))
except Exception as e:
    print(f"Error initializing OpenAI client: {e}")
    client = None

router = APIRouter()

# --- Variation library (small text pools drawn from at random) ---
PROMPT_COMPONENTS = {
    "openingExamples": {
        "contact_person": [
            "Herr {name} hat mich von Anfang an hervorragend begleitet",
            "Frau {name} war mein Ansprechpartner und hat schnell Klarheit geschaffen"
        ],
        "highlight": [
            "Besonders geholfen hat mir {highlight}",
            "Was mich überzeugt hat: {highlight}"
        ],
        "reason": [
            "Ich habe mich wegen {reason} an die Kanzlei gewandt",
            "Anlass war {reason}"
        ],
        "feeling": [
            "Ich fühlte mich durchweg {feeling}",
            "Von Anfang an {feeling}"
        ],
        "neutral": [
            "Sehr gute Erfahrung",
            "Alles in allem eine runde Sache"
        ]
    },
    "transitions": ["Außerdem", "Zudem", "Besonders", "Darüber hinaus", "Nicht zuletzt", "Zusätzlich"],
    "closings": ["kann ich nur empfehlen", "jederzeit wieder", "war die richtige Wahl", "bin sehr zufrieden", "würde ich jederzeit weiterempfehlen"],
    "softCritique": [
        "Die Antwort hätte stellenweise etwas schneller sein können, insgesamt aber top",
        "Kleine Rückfragen wurden zügig geklärt – unterm Strich sehr positiv"
    ],
    "emojiSet": ["😊", "😉", "👍"]
}

# --- Reviewer persona pool (Phase 1 variety engine) ---
# Independent axes drawn at random per review. NOT tied to customer or company.

# Sentence-count corridors. Floor >= 2 so we never emit thin one-liners.
PERSONA_LENGTHS = {
    "kurz": (2, 3),
    "mittel": (3, 6),
    "lang": (6, 9),
}

# tone -> label + optional-element probabilities (transition / emoji / soft critique)
PERSONA_TONES = {
    "sachlich":             {"label": "sachlich und nüchtern",         "transition": 0.30, "emoji": 0.00, "softcritique": 0.10},
    "warm":                 {"label": "warm und persönlich",           "transition": 0.45, "emoji": 0.05, "softcritique": 0.15},
    "begeistert":           {"label": "begeistert und lebendig",       "transition": 0.50, "emoji": 0.10, "softcritique": 0.08},
    "nuechtern_empfehlend": {"label": "ruhig und klar empfehlend",     "transition": 0.35, "emoji": 0.00, "softcritique": 0.10},
}

PERSONA_STRUCTURES = {
    "chronologisch": "chronologisch erzählen (Anlass → Ablauf → Ergebnis)",
    "aspekt_zuerst": "mit einem konkreten Aspekt beginnen und dann ausweiten",
    "fazit_zuerst":  "mit dem Fazit beginnen und es anschließend begründen",
}

# emphasis key -> German label for the aspect that should lead the review
PERSONA_EMPHASES = {
    "grund":           "den Grund der Zusammenarbeit",
    "highlight":       "das Highlight",
    "ansprechpartner": "den Ansprechpartner",
    "gefuehl":         "das Gefühl während der Zusammenarbeit",
}


class GenerateReviewRequest(BaseModel):
    """
    Defines the expected input for the AI review generation endpoint.
    All fields from the feedback form are included to provide context to the AI.
    """
    collaboration_reason: str
    contact_person: str = ""
    collaboration_feeling: str
    highlight: str
    satisfaction: int
    recommendation: str # 'ja', 'nein', 'vielleicht'
    customer_uuid: str
    length: str = "mittel"  # accepted for backwards-compat; length is now randomized server-side
    existing_reviews: list[str] = []  # List of existing review texts for uniqueness check

class GenerateReviewResponse(BaseModel):
    """
    Defines the output of the AI review generation endpoint.
    """
    generated_review: str


def resolve_contact_person_display(contact_person: str) -> str:
    """Resolve contact person to proper display format"""
    if not contact_person or contact_person.lower() in ["jemand anderes", "weiß nicht", "someone else", "don't know"]:
        return ""

    # Simple gender inference by common German first names
    male_names = ["alexander", "andreas", "christian", "daniel", "david", "frank", "jan", "jens", "jörg", "kai", "klaus", "lars", "marc", "marco", "markus", "martin", "matthias", "michael", "oliver", "patrick", "peter", "ralf", "robert", "stefan", "stephan", "thomas", "thorsten", "tim", "tobias", "uwe", "wolfgang"]
    female_names = ["alexandra", "andrea", "angela", "anke", "anna", "antje", "barbara", "birgit", "brigitte", "christina", "christine", "claudia", "daniela", "doris", "eva", "gabriele", "heike", "ines", "jana", "julia", "karin", "katja", "katrin", "kerstin", "kirsten", "manuela", "maria", "marion", "martina", "melanie", "monika", "nadine", "nicole", "petra", "sabine", "sandra", "silke", "simone", "stefanie", "susanne", "tanja", "ute"]

    parts = contact_person.strip().split()
    if len(parts) >= 2:
        first_name = parts[0].lower()
        last_name = " ".join(parts[1:])

        if first_name in male_names:
            return f"Herr {last_name}"
        elif first_name in female_names:
            return f"Frau {last_name}"
        else:
            return contact_person  # Full name if gender unclear

    return contact_person


def build_persona(request: "GenerateReviewRequest", display_contact: str) -> dict:
    """
    Draw an independent random reviewer persona for THIS single review.

    Axes are chosen independently (combinatorial variety) and are not derived from
    the customer UUID or any per-company profile. Emphasis is restricted to aspects
    the guest actually provided, so we never ask the model to lead with a missing field.
    """
    length_band = random.choice(list(PERSONA_LENGTHS.keys()))
    lo, hi = PERSONA_LENGTHS[length_band]
    target_sentences = random.randint(lo, hi)

    tone_key = random.choice(list(PERSONA_TONES.keys()))
    structure_key = random.choice(list(PERSONA_STRUCTURES.keys()))

    available_emphases = []
    if request.collaboration_reason.strip():
        available_emphases.append("grund")
    if request.highlight.strip():
        available_emphases.append("highlight")
    if display_contact:
        available_emphases.append("ansprechpartner")
    if request.collaboration_feeling.strip():
        available_emphases.append("gefuehl")
    emphasis_key = random.choice(available_emphases) if available_emphases else "grund"

    return {
        "length_band": length_band,
        "target_sentences": target_sentences,
        "tone_key": tone_key,
        "structure_key": structure_key,
        "emphasis_key": emphasis_key,
    }


def format_existing_reviews(reviews: list[str]) -> str:
    """Format existing reviews for the prompt"""
    if not reviews:
        return ""

    # Filter out empty reviews and limit to avoid token overflow
    valid_reviews = [r.strip() for r in reviews if r and r.strip()]
    if not valid_reviews:
        return ""

    # Format each review with a number
    formatted = []
    for i, review in enumerate(valid_reviews, 1):
        # Truncate very long reviews to save tokens
        truncated = review[:500] + "..." if len(review) > 500 else review
        formatted.append(f"{i}. \"{truncated}\"")

    return "\n".join(formatted)


@router.post(
    "/generate-review",
    response_model=GenerateReviewResponse,
    summary="Generate AI-powered Google Review",
    description="Receives client feedback and uses a random reviewer-persona prompt to generate natural German reviews."
)
def generate_ai_review(
    request: GenerateReviewRequest,
):
    """
    Takes structured client feedback and generates a natural German Google review.

    Each call draws a fresh random reviewer persona (length, tone, structure, emphasis)
    so that reviews for the same business read as if written by different people.

    Args:
        request: A GenerateReviewRequest object containing all feedback details.

    Returns:
        A GenerateReviewResponse object with the generated review text.

    Raises:
        HTTPException: If the OpenAI client is not available or if the API call fails.
    """
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI client is not configured. Please check API key.")

    min_sat = feedback_high_satisfaction_min()
    if request.satisfaction < min_sat:
        raise HTTPException(
            status_code=400,
            detail="AI review drafts are only available for high-satisfaction feedback.",
        )

    # --- Step 1: Resolve contact person display ---
    display_contact = resolve_contact_person_display(request.contact_person)

    # --- Step 2: Draw a random reviewer persona for this review ---
    persona = build_persona(request, display_contact)
    tone = PERSONA_TONES[persona["tone_key"]]
    lo, hi = PERSONA_LENGTHS[persona["length_band"]]

    # --- Step 3: Apply tone-driven optional elements ---
    use_transition = random.random() < tone["transition"]
    use_emoji = random.random() < tone["emoji"]
    use_soft_critique = random.random() < tone["softcritique"]

    transition = random.choice(PROMPT_COMPONENTS["transitions"]) if use_transition else ""
    emoji = random.choice(PROMPT_COMPONENTS["emojiSet"]) if use_emoji else ""
    soft_critique_text = random.choice(PROMPT_COMPONENTS["softCritique"]) if use_soft_critique else ""

    # --- Step 4: Format existing reviews for uniqueness check ---
    existing_reviews_text = format_existing_reviews(request.existing_reviews)
    has_existing_reviews = bool(existing_reviews_text)

    # --- Step 5: Construct the German prompt ---
    prompt = f"""AUFGABE
Erstelle eine natürliche Google-Bewertung auf Deutsch in ausschließlicher Ich-Perspektive.
Gib NUR den Bewertungstext zurück – keine Einleitung, keine Labels.

EINGABEN
grund_der_zusammenarbeit: {request.collaboration_reason}
ansprechpartner: {display_contact}
gefuehl_waehrend_der_zusammenarbeit: {request.collaboration_feeling}
highlight: {request.highlight}
zufriedenheit_von_5: {request.satisfaction}
wuerde_empfehlen: {request.recommendation}

AUSGABE-BEDINGUNGEN
"wuerde_empfehlen" darf NUR erwähnt werden, wenn Wert = "ja". Bei "nein" oder "vielleicht": ignorieren.
"ansprechpartner" darf NUR verwendet werden, wenn ein konkreter Name angegeben ist.
Negative Details nur dezent und direkt positiv entkräften.

STILVARIATION (zufällige Persona für genau diese Bewertung)
Tonalität: {tone['label']}
Aufbau: {PERSONA_STRUCTURES[persona['structure_key']]}
Schwerpunkt: Beginne bzw. betone {PERSONA_EMPHASES[persona['emphasis_key']]}
Übergänge verwenden: {use_transition}
Emoji erlaubt: {use_emoji}
Sanfte Kritik: {use_soft_critique}

TEXTREGELN
WICHTIG - PERSPEKTIVE: Die Bewertung wird von MIR als Kunde geschrieben. Ausschließlich Ich-Form verwenden!
❌ FALSCH: "Sie haben sich gewandt", "Man wurde beraten", "Der Kunde war zufrieden", "Es wurde geholfen"
✅ RICHTIG: "Ich habe mich gewandt", "Ich wurde beraten", "Ich war zufrieden", "Mir wurde geholfen"
Niemals dritte Person oder passive Formulierungen mit "man/sie/es" verwenden.
Eingaben integrieren: grund_der_zusammenarbeit optional, highlight konkret hervorheben, gefuehl_waehrend_der_zusammenarbeit subtil einbauen wenn positiv/neutral, ansprechpartner nach Regel nennen.
Zufriedenheit implizit ausdrücken („rundum zufrieden", „sehr gute Erfahrung"), keine Sterne nennen.
Satzlängen mischen, Redundanzen vermeiden.

LÄNGE
Ziel: etwa {persona['target_sentences']} Sätze (Korridor {lo}–{hi}). Niemals nur ein knapper Einzeiler – immer mehrere aussagekräftige Sätze.

SPEZIELLE ANWEISUNGEN
{f"Verwende Übergang: {transition}" if use_transition else ""}
{f"Füge Emoji hinzu: {emoji}" if use_emoji else ""}
{f"Sanfte Kritik einbauen: {soft_critique_text}" if use_soft_critique else ""}

{"EINZIGARTIGKEIT - KRITISCH WICHTIG" if has_existing_reviews else ""}
{f'''Hier sind alle bestehenden Bewertungen dieses Unternehmens:
---
{existing_reviews_text}
---

STRENGE REGELN FÜR EINZIGARTIGKEIT:
1. NIEMALS gleiche oder ähnliche Eröffnungssätze wie in bestehenden Bewertungen verwenden
2. NIEMALS gleiche Phrasen, Redewendungen oder Formulierungen kopieren oder paraphrasieren
3. ANDERE Satzstrukturen und Satzmuster verwenden als in den bestehenden Bewertungen
4. ANDERE Wörter und Synonyme wählen - wenn bestehende Bewertungen "kompetent" sagen, nutze z.B. "fachkundig" oder "versiert"
5. ANDEREN Fokus setzen - wenn bestehende Bewertungen den Service loben, betone z.B. die Kommunikation oder Erreichbarkeit
6. Die generierte Bewertung muss sich VOLLSTÄNDIG von allen obigen Bewertungen unterscheiden
7. Bei Ähnlichkeit zu einer bestehenden Bewertung: komplett neu formulieren''' if has_existing_reviews else ""}

DATENFEHLER
Fehlende Eingaben weglassen, ohne Platzhalter oder Entschuldigung.

AUSGABE
Nur den finalen Bewertungstext zurückgeben, ohne Labels, Metadaten oder Anführungszeichen."""

    try:
        # Build system message - add uniqueness instruction if existing reviews provided
        system_message = "Du bist ein Experte für natürliche deutsche Google-Bewertungen. Du schreibst authentische Bewertungen IMMER aus der Ich-Perspektive, als ob DU der Kunde bist. NIEMALS dritte Person (sie/man/er/es) verwenden! Befolge die Regeln exakt und variiere den Stil basierend auf den Vorgaben."

        if has_existing_reviews:
            system_message += " WICHTIG: Dir werden bestehende Bewertungen gezeigt. Deine generierte Bewertung MUSS sich vollständig davon unterscheiden - andere Eröffnung, andere Formulierungen, andere Struktur, andere Wortwahl. Keine Ähnlichkeiten erlaubt!"

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9 if has_existing_reviews else 0.8,  # Higher temperature for more variation when avoiding duplicates
            max_tokens=300,
        )
        generated_text = completion.choices[0].message.content
        if not generated_text:
             raise HTTPException(status_code=500, detail="OpenAI returned an empty response.")

        # Clean up the response to ensure it's just the review
        cleaned_review = generated_text.strip().strip('"').strip("'")

        return GenerateReviewResponse(generated_review=cleaned_review)
    except Exception as e:
        print(f"An error occurred while calling OpenAI API: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate review. Error: {str(e)}")
