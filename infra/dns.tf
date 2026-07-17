# Point <subdomain>.<domain> at the box. This looks up your EXISTING Route 53
# hosted zone (it does not create one) and adds a single A record.
#
# Designed for many future subdomains: each new service is just another
# aws_route53_record in this same zone → the same box's IP, and the reverse proxy
# on the box routes by hostname. Nothing else changes.

data "aws_route53_zone" "primary" {
  name = "${var.domain}."
}

resource "aws_route53_record" "dashboard" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "${var.subdomain}.${var.domain}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.box.public_ip]
}
