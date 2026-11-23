data "cloudflare_zone" "mail" {
  filter = {
    name = var.mail_domain
  }
}

locals {
  zone_id = data.cloudflare_zone.mail.zone_id
}

# Every record is proxied = false: MX and TXT cannot be proxied, and a
# proxied DKIM CNAME would not resolve to SES.

resource "cloudflare_dns_record" "mx" {
  zone_id  = local.zone_id
  name     = var.mail_domain
  type     = "MX"
  content  = "inbound-smtp.${var.region}.amazonaws.com"
  priority = 10
  ttl      = 1
  proxied  = false
}

resource "cloudflare_dns_record" "dkim" {
  count   = 3
  zone_id = local.zone_id
  name    = "${aws_ses_domain_dkim.mail.dkim_tokens[count.index]}._domainkey.${var.mail_domain}"
  type    = "CNAME"
  content = "${aws_ses_domain_dkim.mail.dkim_tokens[count.index]}.dkim.amazonses.com"
  ttl     = 1
  proxied = false
}

# Lets the verification resource confirm the domain without waiting on DKIM.
resource "cloudflare_dns_record" "ses_verification" {
  zone_id = local.zone_id
  name    = "_amazonses.${var.mail_domain}"
  type    = "TXT"
  content = "\"${aws_ses_domain_identity.mail.verification_token}\""
  ttl     = 1
  proxied = false
}

resource "cloudflare_dns_record" "bounce_mx" {
  count    = var.enable_mail_from ? 1 : 0
  zone_id  = local.zone_id
  name     = "bounce.${var.mail_domain}"
  type     = "MX"
  content  = "feedback-smtp.${var.region}.amazonses.com"
  priority = 10
  ttl      = 1
  proxied  = false
}

resource "cloudflare_dns_record" "bounce_spf" {
  count   = var.enable_mail_from ? 1 : 0
  zone_id = local.zone_id
  name    = "bounce.${var.mail_domain}"
  type    = "TXT"
  content = "\"v=spf1 include:amazonses.com ~all\""
  ttl     = 1
  proxied = false
}

resource "cloudflare_dns_record" "dmarc" {
  zone_id = local.zone_id
  name    = "_dmarc.${var.mail_domain}"
  type    = "TXT"
  content = "\"v=DMARC1; p=none;\""
  ttl     = 1
  proxied = false
}
