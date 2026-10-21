"""Reply templates (English + Spanish). Clinical and emergency wording is ONLY ever produced from here."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
QUEUE_NAMES = {
    "en": {"billing": "our billing team", "front_desk": "our front-desk team",
           "clinician_routine": "one of our clinicians", "clinician_urgent": "a clinician, today"},
    "es": {"billing": "nuestro equipo de facturación", "front_desk": "nuestro equipo de recepción",
           "clinician_routine": "uno de nuestros profesionales clínicos",
           "clinician_urgent": "un profesional clínico, hoy mismo"},
}

T: dict[str, dict[str, str]] = {
    "en": {
        "greeting": "Hi {name},", "greeting_anon": "Hello,",
        "signoff": "- Maplewood Family Clinic",
        "emergency": ("This may be a medical emergency. Please call 911 now or go to the nearest emergency "
                      "department. Our clinical team has been alerted to your message."),
        "refill_prepared": ("We've sent your {medication} refill request to your provider for approval. "
                            "We'll let you know once it has been reviewed."),
        "refill_too_early": ("It's a little early to refill {medication}. We can prepare the request from {date} - "
                             "please message us again on or after that date."),
        "refill_needs_visit": ("To refill {medication} we need to see you first, because your last visit was more "
                               "than 12 months ago. We've let your provider know."),
        "refill_needs_visit_slots": " Available appointments: {slots}. Reply with the one that suits you.",
        "refill_to_provider": ("We've passed your {medication} request to your prescribing provider, who will "
                               "review it and follow up with you."),
        "booked": "You're booked: {appt}.",
        "rescheduled": "Your appointment has been moved from {old} to {new}.",
        "cancelled": "We've cancelled your appointment{plural}: {appts}.",
        "alternatives": ("Unfortunately the time you asked for isn't available{why}. We can offer: {slots}. "
                         "Reply with the one that suits you."),
        "new_patient": "Yes, we're accepting new patients. A new-patient visit takes {minutes} minutes.",
        "new_patient_slots": " Upcoming openings: {slots}. Reply with the one you'd like and our front desk will "
                             "help you register.",
        "routed": "We've passed your message to {queue}, who will get back to you.",
        "routed_urgent": "We've asked one of our clinicians to contact you today.",
        "verification": ("To protect your privacy we need to verify your identity before we can help with this. "
                         "Please reply from the phone number or email we have on file, or send your full name and "
                         "date of birth."),
        "decline_disclosure": ("For privacy reasons we can't share information about another person. They are "
                               "welcome to contact us directly, or to add you as an authorised contact."),
        "acknowledge": "Thank you for your message - we're glad we could help.",
        "fallback": "Thank you for your message. A member of our team will review it and get back to you.",
    },
    "es": {
        "greeting": "Hola {name}:", "greeting_anon": "Hola:",
        "signoff": "- Clínica Familiar Maplewood",
        "emergency": ("Esto puede ser una emergencia médica. Llame al 911 ahora o vaya al servicio de urgencias "
                      "más cercano. Nuestro equipo clínico ha sido alertado de su mensaje."),
        "refill_prepared": ("Hemos enviado su solicitud de resurtido de {medication} a su proveedor para su "
                            "aprobación. Le avisaremos cuando la hayan revisado."),
        "refill_too_early": ("Todavía es pronto para resurtir {medication}. Podemos preparar la solicitud a partir "
                             "del {date}; escríbanos de nuevo ese día o después."),
        "refill_needs_visit": ("Para resurtir {medication} necesitamos verle primero, porque su última visita fue "
                               "hace más de 12 meses. Hemos avisado a su proveedor."),
        "refill_needs_visit_slots": " Citas disponibles: {slots}. Responda con la que le convenga.",
        "refill_to_provider": ("Hemos enviado su solicitud de {medication} a su proveedor, quien la revisará y se "
                               "comunicará con usted."),
        "booked": "Su cita está reservada: {appt}.",
        "rescheduled": "Hemos cambiado su cita del {old} al {new}.",
        "cancelled": "Hemos cancelado su cita{plural}: {appts}.",
        "alternatives": ("Lamentablemente el horario que pidió no está disponible{why}. Podemos ofrecerle: {slots}. "
                         "Responda con el que le convenga."),
        "new_patient": "Sí, aceptamos pacientes nuevos. Una visita de paciente nuevo dura {minutes} minutos.",
        "new_patient_slots": " Próximos horarios: {slots}. Responda con el que prefiera y recepción le ayudará "
                             "con el registro.",
        "routed": "Hemos enviado su mensaje a {queue}, que se pondrá en contacto con usted.",
        "routed_urgent": "Hemos pedido a uno de nuestros profesionales clínicos que se comunique con usted hoy.",
        "verification": ("Para proteger su privacidad necesitamos verificar su identidad antes de poder ayudarle. "
                         "Responda desde el teléfono o correo que tenemos registrado, o envíe su nombre completo y "
                         "fecha de nacimiento."),
        "decline_disclosure": ("Por motivos de privacidad no podemos compartir información sobre otra persona. "
                               "Esa persona puede contactarnos directamente o autorizarle como contacto."),
        "acknowledge": "Gracias por su mensaje; nos alegra haber podido ayudarle.",
        "fallback": "Gracias por su mensaje. Un miembro de nuestro equipo lo revisará y le responderá.",
    },
}


def fmt_time(hhmm: str, lang: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    if lang == "es":
        return f"{h:02d}:{m:02d}"
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


def fmt_date(iso: str, lang: str) -> str:
    d = date.fromisoformat(iso)
    if lang == "es":
        return f"{DAYS_ES[d.weekday()]} {d.day} de {MONTHS_ES[d.month - 1]}"
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def fmt_appt(a: dict[str, Any], lang: str) -> str:
    at = "a las" if lang == "es" else "at"
    with_ = "con" if lang == "es" else "with"
    return f"{fmt_date(a['date'], lang)} {at} {fmt_time(a['start'], lang)} {with_} {a['provider_name']}"


def fmt_slots(slots: list[dict[str, Any]], lang: str) -> str:
    sep = " o " if lang == "es" else " or "
    parts = [fmt_appt(s, lang) for s in slots]
    return sep.join([", ".join(parts[:-1]), parts[-1]]) if len(parts) > 1 else (parts[0] if parts else "")


def friendly_reason(problem: str, lang: str) -> str:
    """Turn a scheduling-rule message into patient-friendly wording (no other patients' details)."""
    p = problem.lower()
    m = re.search(r"^(.*?) is on leave (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", problem)
    if m:
        a, b = fmt_date(m.group(2), lang), fmt_date(m.group(3), lang)
        span = a if a == b else (f"{a} - {b}")
        return (f"{m.group(1)} no está disponible ({span})" if lang == "es" else f"{m.group(1)} is away {span}")
    table = [
        ("already has an appointment", "that time is already booked", "ese horario ya está ocupado"),
        ("holiday", "the clinic is closed that day for a holiday", "la clínica está cerrada ese día festivo"),
        ("closed on", "the clinic is closed that day", "la clínica está cerrada ese día"),
        ("lunch", "that time falls in our lunch closure", "ese horario coincide con el cierre del mediodía"),
        ("outside clinic hours", "that's outside our opening hours", "está fuera de nuestro horario"),
        ("in the past", "that time has already passed", "ese horario ya pasó"),
    ]
    for key, en, es in table:
        if key in p:
            return es if lang == "es" else en
    return ""


def emergency_text(lang: str) -> str:
    return T[lang]["emergency"]
