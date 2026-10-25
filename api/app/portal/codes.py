"""The masked e-mail code mail (AA-946 review S251), shared by :mod:`app.portal.public` (writes the message) and
:mod:`app.mail.sender` (puts the code into the mail at send time)."""

# What the stored code mail shows instead of the code (the trip's Messages tab never shows a code).
CODE_MASK = "[code sent to the traveller only]"
